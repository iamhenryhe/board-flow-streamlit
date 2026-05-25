from __future__ import annotations

import argparse
import contextlib
import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pandas as pd
import tushare as ts

logging.getLogger("streamlit.runtime.caching.cache_data_api").setLevel(logging.ERROR)


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = max(0, int(per_minute))
        self.lock = threading.Lock()
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        if self.per_minute <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_minute:
                    self.calls.append(now)
                    return
                sleep_for = max(0.05, 60 - (now - self.calls[0]))
            time.sleep(sleep_for)


def load_app_module() -> ModuleType:
    with contextlib.redirect_stderr(io.StringIO()):
        import app

    return app


def default_trade_date() -> str:
    return datetime.now().strftime("%Y%m%d")


def parse_nodes(nodes_text: str, trade_date: str) -> list[pd.Timestamp]:
    nodes: list[pd.Timestamp] = []
    day = pd.Timestamp(f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}")
    for raw in nodes_text.split(","):
        text = raw.strip()
        if not text:
            continue
        hour, minute = [int(x) for x in text.split(":")]
        nodes.append(day + pd.Timedelta(hours=hour, minutes=minute))
    return sorted(set(nodes))


def output_path(app_module: ModuleType, trade_date: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return app_module.DATA_DIR / f"backfill_rt_min_daily_{trade_date}.csv"


def load_mapping(app_module: ModuleType) -> pd.DataFrame:
    loader = getattr(app_module.load_mapping, "__wrapped__", app_module.load_mapping)
    return loader(str(app_module.KB_PATH))


def make_cumulative_snapshots(
    app_module: ModuleType,
    minutes: pd.DataFrame,
    ts_code: str,
    nodes: list[pd.Timestamp],
    source: str,
) -> pd.DataFrame:
    if minutes is None or minutes.empty:
        return pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS)

    work = minutes.copy()
    work["time"] = pd.to_datetime(work["time"])
    work = work.sort_values("time")

    rows: list[dict] = []
    for node in nodes:
        window = work[work["time"].le(node)]
        if window.empty:
            continue
        rows.append(
            {
                "time": node,
                "ts_code": ts_code,
                "open": float(window["open"].iloc[0]),
                "close": float(window["close"].iloc[-1]),
                "high": float(window["high"].max()),
                "low": float(window["low"].min()),
                "vol": float(window["vol"].sum()),
                "amount": float(window["amount"].sum()),
                "flow_yi": 0.0,
                "source": source,
                "amount_kind": "cumulative",
            }
        )
    return pd.DataFrame(rows, columns=app_module.SNAPSHOT_COLUMNS)


def read_existing_successes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path, usecols=["ts_code"])
    except Exception:
        return set()
    if df.empty:
        return set()
    return set(df["ts_code"].astype(str))


def append_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = not path.exists()
    df.to_csv(path, mode="a", header=header, index=False)


def is_rate_limit_error(text: str) -> bool:
    return "频率超限" in text or "频次" in text or "每分钟最多访问" in text or "每小时最多访问" in text


def run_status(app_module: ModuleType, args: argparse.Namespace) -> None:
    out_path = output_path(app_module, args.date, args.output)
    fail_path = out_path.with_suffix(".failed.csv")
    mapping = load_mapping(app_module)
    total = mapping["ts_code"].nunique()
    if out_path.exists():
        df = pd.read_csv(out_path)
        done = df["ts_code"].nunique() if not df.empty else 0
        rows = len(df)
        times = sorted(pd.to_datetime(df["time"]).dt.strftime("%H:%M").unique().tolist()) if not df.empty else []
    else:
        done = 0
        rows = 0
        times = []
    failed = 0
    if fail_path.exists():
        failed_df = pd.read_csv(fail_path)
        failed = failed_df["ts_code"].nunique() if not failed_df.empty else 0
    print(f"output={out_path}")
    print(f"done_stocks={done}/{total}, rows={rows}, failed_stocks={failed}, times={','.join(times) if times else 'none'}")


def run_merge(app_module: ModuleType, args: argparse.Namespace) -> None:
    out_path = output_path(app_module, args.date, args.output)
    if not out_path.exists():
        raise SystemExit(f"backfill file not found: {out_path}")
    df = pd.read_csv(out_path)
    if df.empty:
        raise SystemExit("backfill file is empty")
    app_module.append_snapshot(df)
    print(f"merged {len(df)} rows from {out_path} into {app_module.SNAPSHOT_PATH}")


def run_backfill(app_module: ModuleType, args: argparse.Namespace) -> None:
    app_module.disable_proxy_env()
    token = app_module.read_token_from_shell()
    if not token:
        raise SystemExit("missing Tushare token")

    nodes = parse_nodes(args.nodes, args.date)
    out_path = output_path(app_module, args.date, args.output)
    fail_path = out_path.with_suffix(".failed.csv")
    mapping = load_mapping(app_module)
    ts_codes = sorted(mapping["ts_code"].drop_duplicates().tolist())
    if args.limit > 0:
        ts_codes = ts_codes[: args.limit]

    done = read_existing_successes(out_path)
    remaining = [code for code in ts_codes if code not in done]
    thread_local = threading.local()
    rate_limiter = RateLimiter(args.rate_per_minute)

    print(
        f"backfill start date={args.date}, nodes={','.join([n.strftime('%H:%M') for n in nodes])}, "
        f"remaining={len(remaining)}/{len(ts_codes)}, workers={args.workers}, "
        f"rate_per_minute={args.rate_per_minute}, output={out_path}",
        flush=True,
    )

    def get_pro():
        if not hasattr(thread_local, "pro"):
            thread_local.pro = ts.pro_api(token)
        return thread_local.pro

    def fetch_one(ts_code: str) -> tuple[str, pd.DataFrame, str]:
        last_error = ""
        attempt = 1
        rate_limit_waits = 0
        while attempt <= args.retries:
            try:
                rate_limiter.wait()
                raw = get_pro().rt_min_daily(ts_code=ts_code, freq="1MIN")
                minutes = app_module.normalize_minute_df(raw)
                snapshots = make_cumulative_snapshots(app_module, minutes, ts_code, nodes, args.source)
                return ts_code, snapshots, ""
            except Exception as exc:
                last_error = str(exc)
                if is_rate_limit_error(last_error) and rate_limit_waits < args.max_rate_limit_waits:
                    rate_limit_waits += 1
                    print(
                        f"rate limited {ts_code}; "
                        f"sleep {args.limit_sleep:.0f}s then retry ({rate_limit_waits}/{args.max_rate_limit_waits})",
                        flush=True,
                    )
                    time.sleep(args.limit_sleep)
                    continue
                if attempt < args.retries:
                    time.sleep(args.retry_sleep)
                    attempt += 1
                else:
                    return ts_code, pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS), last_error
        return ts_code, pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS), last_error

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(fetch_one, code) for code in remaining]
        for index, future in enumerate(as_completed(futures), start=1):
            ts_code, snapshots, error = future.result()
            if not snapshots.empty:
                append_csv(out_path, snapshots)
                print(f"{index}/{len(remaining)} ok {ts_code}: rows={len(snapshots)}", flush=True)
            elif error:
                append_csv(
                    fail_path,
                    pd.DataFrame(
                        [
                            {
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "ts_code": ts_code,
                                "error": error,
                            }
                        ]
                    ),
                )
                print(f"{index}/{len(remaining)} failed {ts_code}: {error}", flush=True)
            else:
                print(f"{index}/{len(remaining)} ok {ts_code}: rows=0", flush=True)
            if args.sleep > 0:
                time.sleep(args.sleep)

    print("backfill finished", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill today's 15-minute nodes with rt_min_daily into a staging CSV.")
    parser.add_argument("--date", default=default_trade_date(), help="YYYYMMDD")
    parser.add_argument("--nodes", default="09:30,09:45,10:00,10:15")
    parser.add_argument("--output", default=None)
    parser.add_argument("--source", default="rt_k", help="source label to write; default keeps current app mode readable")
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=3.0)
    parser.add_argument("--limit-sleep", type=float, default=70.0)
    parser.add_argument("--max-rate-limit-waits", type=int, default=2)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--rate-per-minute", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="debug only: first N stocks")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--merge", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app_module = load_app_module()
    if args.status:
        run_status(app_module, args)
    elif args.merge:
        run_merge(app_module, args)
    else:
        run_backfill(app_module, args)


if __name__ == "__main__":
    main()
