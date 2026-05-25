from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from types import ModuleType
import time
from datetime import datetime, time as dt_time

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


@contextlib.contextmanager
def collector_lock(app_module: ModuleType):
    lock_path = app_module.DATA_DIR / "collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    acquired = False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def in_trading_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.replace(second=0, microsecond=0).time()
    return dt_time(9, 30) <= current <= dt_time(11, 30) or dt_time(13, 0) <= current <= dt_time(15, 0)


def is_collection_node(now: datetime) -> bool:
    now = now.replace(second=0, microsecond=0)
    current = now.time()
    sessions = ((dt_time(9, 30), dt_time(11, 30)), (dt_time(13, 0), dt_time(15, 0)))
    for start, end in sessions:
        if start <= current <= end:
            minutes = (now.hour - start.hour) * 60 + now.minute - start.minute
            return minutes % 15 == 0
    return False


def load_app_module() -> ModuleType:
    with contextlib.redirect_stderr(io.StringIO()):
        import app

    return app


def fetch_rt_min_snapshot(
    app_module: ModuleType,
    token: str,
    ts_codes: list[str],
    freq: str,
    batch_size: int,
    retries: int,
    retry_sleep: float,
) -> tuple[pd.DataFrame, list[str]]:
    app_module.disable_proxy_env()
    pro = ts.pro_api(token)
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    batches = list(app_module.chunked(ts_codes, batch_size))

    for batch_index, batch in enumerate(batches, start=1):
        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                df = pro.rt_min(ts_code=",".join(batch), freq=freq)
                normalized = app_module.normalize_rt_min_snapshot(df)
                frames.append(normalized)
                returned = set(normalized["ts_code"].astype(str))
                batch_failed = [code for code in batch if code not in returned]
                failed.extend(batch_failed)
                print(
                    f"rt_min batch {batch_index}/{len(batches)} ok: "
                    f"requested={len(batch)} returned={len(returned)} failed={len(batch_failed)}",
                    flush=True,
                )
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt < retries:
                    print(
                        f"rt_min batch {batch_index}/{len(batches)} retry {attempt}/{retries}: {last_error}",
                        flush=True,
                    )
                    time.sleep(retry_sleep)
                else:
                    print(
                        f"rt_min batch {batch_index}/{len(batches)} failed: {last_error}",
                        flush=True,
                    )
                    failed.extend(batch)

    if not frames:
        return pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS), failed
    snapshot = pd.concat(frames, ignore_index=True).drop_duplicates(["time", "ts_code"], keep="last")
    return snapshot, failed


def is_rate_limit_error(text: str) -> bool:
    return "频率超限" in text or "频次" in text or "每分钟最多访问" in text or "每小时最多访问" in text


def make_cumulative_node_snapshot(
    app_module: ModuleType,
    minutes: pd.DataFrame,
    ts_code: str,
    node_time: pd.Timestamp,
    source: str,
) -> pd.DataFrame:
    if minutes is None or minutes.empty:
        return pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS)

    work = minutes.copy()
    work["time"] = pd.to_datetime(work["time"])
    work = work.sort_values("time")
    window = work[work["time"].le(node_time)]
    if window.empty:
        return pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS)

    return pd.DataFrame(
        [
            {
                "time": node_time,
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
        ],
        columns=app_module.SNAPSHOT_COLUMNS,
    )


def fetch_rt_min_daily_node_snapshot(
    app_module: ModuleType,
    token: str,
    ts_codes: list[str],
    node_time: pd.Timestamp,
    source: str,
    retries: int,
    retry_sleep: float,
    limit_sleep: float,
    max_rate_limit_waits: int,
    workers: int,
    rate_per_minute: int,
) -> tuple[pd.DataFrame, list[str]]:
    app_module.disable_proxy_env()
    thread_local = threading.local()
    rate_limiter = RateLimiter(rate_per_minute)
    frames: list[pd.DataFrame] = []
    failed: list[str] = []

    def get_pro():
        if not hasattr(thread_local, "pro"):
            thread_local.pro = ts.pro_api(token)
        return thread_local.pro

    def fetch_one(ts_code: str) -> tuple[str, pd.DataFrame, str]:
        attempt = 1
        rate_limit_waits = 0
        last_error = ""
        while attempt <= retries:
            try:
                rate_limiter.wait()
                raw = get_pro().rt_min_daily(ts_code=ts_code, freq="1MIN")
                minutes = app_module.normalize_minute_df(raw)
                snapshot = make_cumulative_node_snapshot(app_module, minutes, ts_code, node_time, source)
                return ts_code, snapshot, ""
            except Exception as exc:
                last_error = str(exc)
                if is_rate_limit_error(last_error) and rate_limit_waits < max_rate_limit_waits:
                    rate_limit_waits += 1
                    print(
                        f"rt_min_daily fallback rate limited {ts_code}; "
                        f"sleep {limit_sleep:.0f}s then retry ({rate_limit_waits}/{max_rate_limit_waits})",
                        flush=True,
                    )
                    time.sleep(limit_sleep)
                    continue
                if attempt < retries:
                    time.sleep(retry_sleep)
                    attempt += 1
                else:
                    return ts_code, pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS), last_error
        return ts_code, pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS), last_error

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, code) for code in ts_codes]
        for index, future in enumerate(as_completed(futures), start=1):
            ts_code, snapshot, error = future.result()
            if snapshot.empty:
                failed.append(ts_code)
                if error:
                    print(f"rt_min_daily fallback failed {ts_code}: {error}", flush=True)
            else:
                frames.append(snapshot)
            if index % 100 == 0 or index == len(ts_codes):
                print(
                    f"rt_min_daily fallback progress: {index}/{len(ts_codes)}, "
                    f"rows={sum(len(frame) for frame in frames)}, failed={len(set(failed))}",
                    flush=True,
                )

    if not frames:
        return pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS), failed
    snapshot = pd.concat(frames, ignore_index=True).drop_duplicates(["time", "ts_code"], keep="last")
    return snapshot, failed


def required_success_count(total: int, min_success_ratio: float) -> int:
    return int(total * min_success_ratio)


def snapshot_stock_count(snapshot: pd.DataFrame) -> int:
    if snapshot.empty or "ts_code" not in snapshot.columns:
        return 0
    return int(snapshot["ts_code"].nunique())


def fetch_rt_k_with_backup(
    app_module: ModuleType,
    token: str,
    ts_codes: list[str],
    now: datetime,
    attempts: int,
    retry_sleep: float,
    min_success_ratio: float,
    fallback: str,
    fallback_retries: int,
    fallback_retry_sleep: float,
    fallback_limit_sleep: float,
    fallback_max_rate_limit_waits: int,
    fallback_workers: int,
    fallback_rate_per_minute: int,
) -> tuple[pd.DataFrame, list[str], str]:
    min_required = required_success_count(len(ts_codes), min_success_ratio)
    best_snapshot = pd.DataFrame(columns=app_module.SNAPSHOT_COLUMNS)
    best_failed: list[str] = ts_codes.copy()

    for attempt in range(1, max(1, attempts) + 1):
        snapshot, failed = app_module.fetch_rt_k_snapshot(token, ts_codes, ignore_proxy=True)
        returned = snapshot_stock_count(snapshot)
        if returned > snapshot_stock_count(best_snapshot):
            best_snapshot = snapshot
            best_failed = failed
        print(
            f"rt_k attempt {attempt}/{max(1, attempts)}: returned={returned}/{len(ts_codes)}, "
            f"min_required={min_required}",
            flush=True,
        )
        if returned >= min_required:
            return snapshot, failed, "rt_k"
        if attempt < max(1, attempts):
            time.sleep(max(0.0, retry_sleep))

    if fallback != "rt_min_daily":
        return best_snapshot, best_failed, "rt_k_incomplete"

    node_time = app_module.normalize_snapshot_node_time(pd.Timestamp(now))
    print(
        f"rt_k incomplete after {max(1, attempts)} attempts; "
        f"fallback to rt_min_daily for {node_time:%Y-%m-%d %H:%M}",
        flush=True,
    )
    snapshot, failed = fetch_rt_min_daily_node_snapshot(
        app_module,
        token,
        ts_codes,
        node_time,
        source="rt_k",
        retries=max(1, fallback_retries),
        retry_sleep=max(0.0, fallback_retry_sleep),
        limit_sleep=max(0.0, fallback_limit_sleep),
        max_rate_limit_waits=max(0, fallback_max_rate_limit_waits),
        workers=max(1, fallback_workers),
        rate_per_minute=max(0, fallback_rate_per_minute),
    )
    return snapshot, failed, "rt_min_daily_backup"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect sector-flow snapshots into data/minute_snapshots.csv")
    parser.add_argument("--source", choices=["rt_k", "rt_min"], default="rt_k")
    parser.add_argument("--freq", choices=["1MIN", "5MIN", "15MIN", "30MIN", "60MIN"], default="15MIN")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--min-success-ratio", type=float, default=0.95)
    parser.add_argument("--rt-k-attempts", type=int, default=3)
    parser.add_argument("--fallback", choices=["rt_min_daily", "none"], default="rt_min_daily")
    parser.add_argument("--fallback-retries", type=int, default=2)
    parser.add_argument("--fallback-retry-sleep", type=float, default=1.0)
    parser.add_argument("--fallback-limit-sleep", type=float, default=70.0)
    parser.add_argument("--fallback-max-rate-limit-waits", type=int, default=1)
    parser.add_argument("--fallback-workers", type=int, default=1)
    parser.add_argument("--fallback-rate-per-minute", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="debug only: collect first N stocks")
    parser.add_argument("--allow-off-node", action="store_true", help="debug only: save non-15-minute snapshots")
    parser.add_argument("--force", action="store_true", help="run even outside A-share trading hours")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = datetime.now()
    if not args.force and not in_trading_window(now):
        print(f"skip: {now:%Y-%m-%d %H:%M:%S} is outside A-share trading window", flush=True)
        return
    if args.source == "rt_k" and not args.allow_off_node and not is_collection_node(now):
        print(f"skip: {now:%Y-%m-%d %H:%M:%S} is not a 15-minute node", flush=True)
        return

    app_module = load_app_module()
    with collector_lock(app_module) as acquired:
        if not acquired:
            print(f"skip: {now:%Y-%m-%d %H:%M:%S} another collector run is still active", flush=True)
            return

        token = app_module.read_token_from_shell()
        if not token:
            raise SystemExit("missing Tushare token")

        load_mapping = getattr(app_module.load_mapping, "__wrapped__", app_module.load_mapping)
        mapping = load_mapping(str(app_module.KB_PATH))
        ts_codes = sorted(mapping["ts_code"].drop_duplicates().tolist())
        if args.limit > 0:
            ts_codes = ts_codes[: args.limit]

        started = time.time()
        print(
            f"start {args.source}: {now:%Y-%m-%d %H:%M:%S}, stocks={len(ts_codes)}, output={app_module.SNAPSHOT_PATH}",
            flush=True,
        )

        used_source = args.source
        if args.source == "rt_k":
            snapshot, failed, used_source = fetch_rt_k_with_backup(
                app_module,
                token,
                ts_codes,
                now,
                attempts=args.rt_k_attempts,
                retry_sleep=args.retry_sleep,
                min_success_ratio=args.min_success_ratio,
                fallback=args.fallback,
                fallback_retries=args.fallback_retries,
                fallback_retry_sleep=args.fallback_retry_sleep,
                fallback_limit_sleep=args.fallback_limit_sleep,
                fallback_max_rate_limit_waits=args.fallback_max_rate_limit_waits,
                fallback_workers=args.fallback_workers,
                fallback_rate_per_minute=args.fallback_rate_per_minute,
            )
        else:
            snapshot, failed = fetch_rt_min_snapshot(
                app_module,
                token,
                ts_codes,
                freq=args.freq,
                batch_size=args.batch_size,
                retries=max(1, args.retries),
                retry_sleep=max(0.0, args.retry_sleep),
            )

        if args.source == "rt_k" and ts_codes and args.min_success_ratio > 0:
            returned_count = snapshot_stock_count(snapshot)
            min_required = required_success_count(len(ts_codes), args.min_success_ratio)
            if returned_count < min_required:
                elapsed = time.time() - started
                print(
                    f"incomplete snapshot: source={used_source}, returned={returned_count}/{len(ts_codes)}, "
                    f"min_required={min_required}, not saved, elapsed={elapsed:.1f}s",
                    flush=True,
                )
                return

        app_module.append_snapshot(snapshot)
        elapsed = time.time() - started

        if snapshot.empty:
            print(f"empty snapshot: source={used_source}, failed={len(failed)}, elapsed={elapsed:.1f}s", flush=True)
            return

        times = ", ".join(pd.to_datetime(snapshot["time"]).dt.strftime("%Y-%m-%d %H:%M").drop_duplicates().tolist())
        print(
            f"saved: source={used_source}, times={times}, rows={len(snapshot)}, "
            f"failed={len(set(failed))}, elapsed={elapsed:.1f}s",
            flush=True,
        )
if __name__ == "__main__":
    main()
