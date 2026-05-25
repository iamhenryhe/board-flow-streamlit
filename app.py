import base64
import html
import os
import re
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import requests.sessions
import streamlit as st
import tushare as ts


APP_DIR = Path(__file__).resolve().parent
KB_PATH = APP_DIR / "kb.xlsx"
DATA_DIR = APP_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "minute_snapshots.csv"
RT_K_FULL_MARKET = "3*.SZ,6*.SH,0*.SZ,9*.BJ"
RT_K_MARKET_PATTERNS = ["0*.SZ", "3*.SZ", "6*.SH", "9*.BJ"]
SNAPSHOT_INTERVAL_MINUTES = 15
TOP_LINE_COLORS = ["#E60012", "#0052CC", "#00A86B", "#7B2CBF", "#FF8C00"]
BACKGROUND_LINE_COLORS = ["#C8CDD5", "#D9DEE7", "#BFC7D2", "#E0E3E8"]
ORIGINAL_REQUESTS_MERGE = requests.sessions.Session.merge_environment_settings
SNAPSHOT_COLUMNS = [
    "time",
    "ts_code",
    "open",
    "close",
    "high",
    "low",
    "vol",
    "amount",
    "flow_yi",
    "source",
    "amount_kind",
]


@dataclass(frozen=True)
class BoardSeries:
    board: str
    points: pd.Series
    stock_count: int
    failed_count: int


def read_token_from_shell() -> str | None:
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        return token.strip()

    try:
        token = ts.get_token()
        if token:
            return token.strip()
    except Exception:
        pass

    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        match = re.search(r'TUSHARE_TOKEN=["\']?([^"\'\n]+)', zshrc.read_text(errors="ignore"))
        if match:
            return match.group(1).strip()
    return None


def disable_proxy_env() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        settings = ORIGINAL_REQUESTS_MERGE(self, url, proxies, stream, verify, cert)
        settings["proxies"] = {}
        return settings

    requests.sessions.Session.merge_environment_settings = merge_environment_settings


def normalize_code(code: object) -> str | None:
    if pd.isna(code):
        return None
    text = str(code).strip()
    if not text:
        return None
    text = re.sub(r"\.0$", "", text)
    text = re.sub(r"\D", "", text)
    if not text:
        return None
    return text.zfill(6)[-6:]


def code_to_ts(code: str) -> str | None:
    code = normalize_code(code)
    if code is None:
        return None
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return None


@st.cache_data(show_spinner=False)
def load_mapping(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, dtype={"代码": str})
    required = {"板块", "简称", "代码"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"kb.xlsx 缺少列: {', '.join(sorted(missing))}")

    out = df.loc[:, ["板块", "简称", "代码"]].copy()
    out["板块"] = out["板块"].astype(str).str.strip()
    out["简称"] = out["简称"].astype(str).str.strip()
    out["代码"] = out["代码"].map(normalize_code)
    out["ts_code"] = out["代码"].map(code_to_ts)
    out = out.dropna(subset=["板块", "代码", "ts_code"])
    out = out[out["板块"].ne("")]
    out = out.drop_duplicates(["板块", "ts_code"])
    return out


@st.cache_data(ttl=45, show_spinner=False)
def fetch_rt_min_daily(token: str, ts_code: str, ignore_proxy: bool) -> pd.DataFrame:
    if ignore_proxy:
        disable_proxy_env()
    pro = ts.pro_api(token)
    df = pro.rt_min_daily(ts_code=ts_code, freq="1MIN")
    return normalize_minute_df(df)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_stk_mins(token: str, ts_code: str, start: str, end: str, ignore_proxy: bool) -> pd.DataFrame:
    if ignore_proxy:
        disable_proxy_env()
    pro = ts.pro_api(token)
    df = pro.stk_mins(ts_code=ts_code, freq="1min", start_date=start, end_date=end)
    return normalize_minute_df(df)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def get_recent_trade_dates(token: str, end_date: str, count: int, ignore_proxy: bool) -> list[str]:
    if ignore_proxy:
        disable_proxy_env()
    pro = ts.pro_api(token)
    start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=max(12, count * 4))).strftime("%Y%m%d")
    cal = pro.trade_cal(exchange="", start_date=start, end_date=end_date, is_open="1")
    if cal.empty:
        return [end_date]
    dates = cal["cal_date"].astype(str).sort_values().tail(count).tolist()
    return dates


def normalize_minute_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["time", "open", "close", "high", "low", "vol", "amount"])

    out = df.copy()
    if "trade_time" in out.columns and "time" not in out.columns:
        out = out.rename(columns={"trade_time": "time"})
    out["time"] = pd.to_datetime(out["time"])
    for col in ("open", "close", "high", "low", "vol", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["time", "open", "close", "amount"]).sort_values("time")
    return out.loc[:, ["time", "open", "close", "high", "low", "vol", "amount"]]


def normalize_a_share_snapshot_time(ts_value: pd.Timestamp) -> pd.Timestamp:
    ts_value = pd.Timestamp(ts_value).floor("min")
    day = ts_value.normalize()
    morning_open = day + pd.Timedelta(hours=9, minutes=30)
    morning_close = day + pd.Timedelta(hours=11, minutes=30)
    afternoon_open = day + pd.Timedelta(hours=13)
    market_close = day + pd.Timedelta(hours=15)

    if ts_value < morning_open:
        return morning_open
    if morning_close < ts_value < afternoon_open:
        return morning_close
    if ts_value > market_close:
        return market_close
    return ts_value


def normalize_snapshot_node_time(ts_value: pd.Timestamp) -> pd.Timestamp:
    ts_value = normalize_a_share_snapshot_time(ts_value)
    day = ts_value.normalize()
    sessions = (
        (day + pd.Timedelta(hours=9, minutes=30), day + pd.Timedelta(hours=11, minutes=30)),
        (day + pd.Timedelta(hours=13), day + pd.Timedelta(hours=15)),
    )
    for start, end in sessions:
        if start <= ts_value <= end:
            minutes = int((ts_value - start).total_seconds() // 60)
            snapped = (minutes // SNAPSHOT_INTERVAL_MINUTES) * SNAPSHOT_INTERVAL_MINUTES
            return start + pd.Timedelta(minutes=snapped)
    return ts_value


def is_snapshot_node_time(ts_value: pd.Timestamp) -> bool:
    ts_value = normalize_a_share_snapshot_time(ts_value)
    day = ts_value.normalize()
    sessions = (
        (day + pd.Timedelta(hours=9, minutes=30), day + pd.Timedelta(hours=11, minutes=30)),
        (day + pd.Timedelta(hours=13), day + pd.Timedelta(hours=15)),
    )
    for start, end in sessions:
        if start <= ts_value <= end:
            minutes = int((ts_value - start).total_seconds() // 60)
            return minutes % SNAPSHOT_INTERVAL_MINUTES == 0
    return False


def filter_snapshot_nodes(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df["time"].map(is_snapshot_node_time)]


def snapshot_dedup_columns(df: pd.DataFrame) -> list[str]:
    if "source" in df.columns:
        return ["time", "ts_code", "source"]
    return ["time", "ts_code"]


def normalize_rt_min_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    out = df.copy()
    if "code" in out.columns and "ts_code" not in out.columns:
        out = out.rename(columns={"code": "ts_code"})
    out["time"] = pd.to_datetime(out["time"])
    out["ts_code"] = out["ts_code"].astype(str)
    for col in ("open", "close", "high", "low", "vol", "amount"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["time", "ts_code", "open", "close", "amount"])
    direction = np.sign(out["close"] - out["open"])
    out["flow_yi"] = direction.fillna(0) * out["amount"].fillna(0) / 1e8
    out["source"] = "rt_min"
    out["amount_kind"] = "bar"
    return out.loc[:, SNAPSHOT_COLUMNS]


def normalize_realtime_quote_snapshot(df: pd.DataFrame, snapshot_time: pd.Timestamp) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    out = df.copy()
    out.columns = [str(col).lower() for col in out.columns]
    out = out.rename(columns={"price": "close", "volume": "vol"})
    if "ts_code" not in out.columns:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    out["time"] = pd.Timestamp(snapshot_time).floor("min")
    out["ts_code"] = out["ts_code"].astype(str)
    for col in ("open", "close", "high", "low", "vol", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        else:
            out[col] = np.nan
    out = out.dropna(subset=["time", "ts_code", "open", "close", "amount"])
    out["flow_yi"] = 0.0
    out["source"] = "realtime_quote"
    out["amount_kind"] = "cumulative"
    return out.loc[:, SNAPSHOT_COLUMNS]


def normalize_rt_k_snapshot(df: pd.DataFrame, snapshot_time: pd.Timestamp) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    out = df.copy()
    if "trade_time" in out.columns:
        parsed_time = pd.to_datetime(out["trade_time"], errors="coerce")
        out["time"] = parsed_time.fillna(pd.Timestamp(snapshot_time).floor("min"))
    else:
        out["time"] = pd.Timestamp(snapshot_time).floor("min")

    out["ts_code"] = out["ts_code"].astype(str)
    for col in ("open", "close", "high", "low", "vol", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        else:
            out[col] = np.nan
    out = out.dropna(subset=["time", "ts_code", "open", "close", "amount"])
    out["flow_yi"] = 0.0
    out["source"] = "rt_k"
    out["amount_kind"] = "cumulative"
    return out.loc[:, SNAPSHOT_COLUMNS]


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def selected_mapping(mapping: pd.DataFrame, boards: list[str], max_stocks: int) -> pd.DataFrame:
    scoped = mapping[mapping["板块"].isin(boards)].drop_duplicates(["板块", "ts_code"])
    return scoped.groupby("板块", group_keys=False).head(max_stocks)


def source_for_mode(data_mode: str) -> str | None:
    if data_mode == "Tushare rt_k 全市场快照（推荐）":
        return "rt_k"
    if data_mode == "全市场盘口快照（备用，Tushare 爬虫源）":
        return "realtime_quote"
    if data_mode == "Tushare Pro 实时分钟快照（受频控）":
        return "rt_min"
    return None


def cumulative_mode(data_mode: str) -> bool:
    return data_mode in (
        "Tushare rt_k 全市场快照（推荐）",
        "全市场盘口快照（备用，Tushare 爬虫源）",
    )


def fetch_rt_k_snapshot(
    token: str,
    ts_codes: list[str],
    ignore_proxy: bool,
    progress_cb=None,
) -> tuple[pd.DataFrame, list[str]]:
    if ignore_proxy:
        disable_proxy_env()
    pro = ts.pro_api(token)

    snapshot_time = normalize_a_share_snapshot_time(pd.Timestamp.now())
    frames: list[pd.DataFrame] = []
    pattern_errors: list[str] = []

    for idx, pattern in enumerate(RT_K_MARKET_PATTERNS, start=1):
        if progress_cb:
            progress_cb(idx, len(RT_K_MARKET_PATTERNS), 0)
        try:
            df = pro.rt_k(ts_code=pattern)
            frames.append(normalize_rt_k_snapshot(df, snapshot_time))
        except Exception:
            pattern_errors.append(pattern)

    if frames:
        normalized = pd.concat(frames, ignore_index=True)
    else:
        df = pro.rt_k(ts_code=RT_K_FULL_MARKET)
        normalized = normalize_rt_k_snapshot(df, snapshot_time)

    if ts_codes:
        wanted = set(ts_codes)
        normalized = normalized[normalized["ts_code"].isin(wanted)]
        returned = set(normalized["ts_code"].astype(str))
        failed = [code for code in ts_codes if code not in returned]
    else:
        failed = []
    if pattern_errors and failed:
        failed = sorted(set(failed))
    return normalized.drop_duplicates(["time", "ts_code"], keep="last"), failed


def fetch_rt_min_snapshot_batch(
    token: str,
    ts_codes: list[str],
    batch_size: int,
    ignore_proxy: bool,
    progress_cb=None,
) -> tuple[pd.DataFrame, list[str]]:
    if ignore_proxy:
        disable_proxy_env()
    pro = ts.pro_api(token)
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    batches = list(chunked(ts_codes, batch_size))

    for batch_index, batch in enumerate(batches, start=1):
        if progress_cb:
            progress_cb(batch_index, len(batches), len(batch))
        try:
            df = pro.rt_min(ts_code=",".join(batch), freq="1MIN")
            normalized = normalize_rt_min_snapshot(df)
            frames.append(normalized)
            returned = set(normalized["ts_code"].astype(str))
            failed.extend([code for code in batch if code not in returned])
        except Exception:
            failed.extend(batch)

    if not frames:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS), failed
    return pd.concat(frames, ignore_index=True).drop_duplicates(["time", "ts_code"], keep="last"), failed


def fetch_realtime_quote_snapshot_batch(
    token: str,
    ts_codes: list[str],
    batch_size: int,
    workers: int,
    ignore_proxy: bool,
    progress_cb=None,
) -> tuple[pd.DataFrame, list[str]]:
    if ignore_proxy:
        disable_proxy_env()
    if token:
        ts.set_token(token)

    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    batches = list(chunked(ts_codes, batch_size))
    snapshot_time = normalize_a_share_snapshot_time(pd.Timestamp.now())

    def fetch_one(batch: list[str]) -> tuple[pd.DataFrame, list[str], int]:
        df = ts.realtime_quote(ts_code=",".join(batch), src="sina")
        normalized = normalize_realtime_quote_snapshot(df, snapshot_time)
        returned = set(normalized["ts_code"].astype(str))
        batch_failed = [code for code in batch if code not in returned]
        return normalized, batch_failed, len(batch)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_one, batch): batch for batch in batches}
        for done, future in enumerate(as_completed(futures), start=1):
            batch = futures[future]
            try:
                normalized, batch_failed, batch_len = future.result()
                frames.append(normalized)
                failed.extend(batch_failed)
            except Exception:
                batch_len = len(batch)
                failed.extend(batch)
            if progress_cb:
                progress_cb(done, len(batches), batch_len)

    if not frames:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS), failed
    return pd.concat(frames, ignore_index=True).drop_duplicates(["time", "ts_code"], keep="last"), failed


def append_snapshot(snapshot: pd.DataFrame) -> pd.DataFrame:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if snapshot.empty:
        return load_snapshots([])

    out = snapshot.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    if SNAPSHOT_PATH.exists():
        old = pd.read_csv(SNAPSHOT_PATH)
        combined = pd.concat([old, out], ignore_index=True)
    else:
        combined = out
    combined["time"] = pd.to_datetime(combined["time"]).map(normalize_a_share_snapshot_time)
    combined = combined.drop_duplicates(snapshot_dedup_columns(combined), keep="last").sort_values(["time", "ts_code"])
    combined.to_csv(SNAPSHOT_PATH, index=False)
    combined["time"] = pd.to_datetime(combined["time"])
    return combined


def load_snapshots(trade_dates: list[str]) -> pd.DataFrame:
    if not SNAPSHOT_PATH.exists():
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    df = pd.read_csv(SNAPSHOT_PATH)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"])
    df["time"] = df["time"].map(normalize_a_share_snapshot_time)
    df = df.drop_duplicates(snapshot_dedup_columns(df), keep="last").sort_values(["time", "ts_code"])
    if trade_dates:
        wanted = set(trade_dates)
        df = df[df["time"].dt.strftime("%Y%m%d").isin(wanted)]
    for col in ("open", "close", "high", "low", "vol", "amount", "flow_yi"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "source" not in df.columns:
        df["source"] = "unknown"
    if "amount_kind" not in df.columns:
        df["amount_kind"] = "bar"
    return df


def prepare_snapshot_flows(snapshots: pd.DataFrame) -> pd.DataFrame:
    if snapshots.empty:
        return snapshots

    out = snapshots.copy()
    out["time"] = pd.to_datetime(out["time"])
    for col in ("open", "close", "amount", "flow_yi"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["trade_date"] = out["time"].dt.strftime("%Y%m%d")
    if "amount_kind" in out.columns:
        out["amount_kind"] = out["amount_kind"].fillna("bar")
    else:
        out["amount_kind"] = "bar"

    cumulative_mask = out["amount_kind"].eq("cumulative")
    if cumulative_mask.any():
        cum = out.loc[cumulative_mask].sort_values(["ts_code", "trade_date", "time"]).copy()
        prev_amount = cum.groupby(["ts_code", "trade_date"])["amount"].shift(1)
        prev_close = cum.groupby(["ts_code", "trade_date"])["close"].shift(1)
        delta_amount = (cum["amount"] - prev_amount).where(prev_amount.notna(), cum["amount"])
        delta_amount = delta_amount.where(delta_amount.ge(0), 0).fillna(0)
        direction = np.sign(cum["close"] - prev_close)
        direction = direction.where(prev_close.notna(), np.sign(cum["close"] - cum["open"]))
        out.loc[cum.index, "flow_yi"] = direction.fillna(0) * delta_amount / 1e8

    bar_mask = ~cumulative_mask
    missing_flow = out["flow_yi"].isna()
    if (bar_mask & missing_flow).any():
        idx = out.index[bar_mask & missing_flow]
        direction = np.sign(out.loc[idx, "close"] - out.loc[idx, "open"])
        out.loc[idx, "flow_yi"] = direction.fillna(0) * out.loc[idx, "amount"].fillna(0) / 1e8

    return out.drop(columns=["trade_date"])


def aggregate_boards_from_snapshots(
    mapping: pd.DataFrame,
    snapshots: pd.DataFrame,
    boards: list[str],
    max_stocks: int,
    failed_codes: set[str] | None = None,
) -> list[BoardSeries]:
    scoped = selected_mapping(mapping, boards, max_stocks)
    if snapshots.empty or scoped.empty:
        return []

    snapshots = prepare_snapshot_flows(snapshots)
    merged = snapshots.merge(scoped.loc[:, ["板块", "ts_code"]], on="ts_code", how="inner")
    if merged.empty:
        return []

    cumulative_source = "amount_kind" in snapshots.columns and snapshots["amount_kind"].eq("cumulative").any()
    grouped = (
        merged.groupby(["板块", "time"], as_index=False)["flow_yi"]
        .sum()
        .sort_values(["板块", "time"])
    )

    results: list[BoardSeries] = []
    failed_codes = failed_codes or set()
    for board in boards:
        board_df = grouped[grouped["板块"].eq(board)]
        board_codes = set(scoped.loc[scoped["板块"].eq(board), "ts_code"])
        if board_df.empty:
            results.append(BoardSeries(board=board, points=pd.Series(dtype=float), stock_count=len(board_codes), failed_count=len(board_codes)))
            continue
        points = pd.Series(
            board_df["flow_yi"].to_numpy(),
            index=pd.to_datetime(board_df["time"]),
        ).sort_index().cumsum()
        if cumulative_source and not points.empty:
            first_time = pd.Timestamp(points.index.min())
            market_open = first_time.normalize() + pd.Timedelta(hours=9, minutes=30)
            if first_time > market_open:
                points = pd.concat([pd.Series([0.0], index=[market_open]), points]).sort_index()
        failed_count = len(board_codes & failed_codes)
        results.append(BoardSeries(board=board, points=points, stock_count=len(board_codes), failed_count=failed_count))
    return results


def signed_minute_amount(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)

    work = df.sort_values("time").copy()
    prev_close = work["close"].shift(1).fillna(work["open"])
    direction = np.sign(work["close"] - prev_close)
    zero_mask = direction.eq(0)
    direction.loc[zero_mask] = np.sign(work.loc[zero_mask, "close"] - work.loc[zero_mask, "open"])
    signed_yi = direction.fillna(0) * work["amount"].fillna(0) / 1e8
    return pd.Series(signed_yi.to_numpy(), index=work["time"])


def get_stock_minutes(
    token: str,
    ts_code: str,
    trade_dates: list[str],
    ignore_proxy: bool,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    today = datetime.now().strftime("%Y%m%d")
    for d in trade_dates:
        if d == today:
            frames.append(fetch_rt_min_daily(token, ts_code, ignore_proxy))
        else:
            start = f"{d[:4]}-{d[4:6]}-{d[6:]} 09:30:00"
            end = f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00:00"
            frames.append(fetch_stk_mins(token, ts_code, start, end, ignore_proxy))
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates("time").sort_values("time")


def aggregate_board(
    token: str,
    mapping: pd.DataFrame,
    board: str,
    trade_dates: list[str],
    max_stocks: int,
    ignore_proxy: bool,
    progress_cb=None,
) -> BoardSeries:
    rows = mapping[mapping["板块"].eq(board)].drop_duplicates("ts_code")
    rows = rows.head(max_stocks)

    minute_parts: list[pd.Series] = []
    failed = 0
    total = len(rows)
    for i, row in enumerate(rows.itertuples(index=False), start=1):
        if progress_cb:
            progress_cb(board, i, total, row.ts_code)
        try:
            minutes = get_stock_minutes(token, row.ts_code, trade_dates, ignore_proxy)
            series = signed_minute_amount(minutes)
            if not series.empty:
                minute_parts.append(series)
            else:
                failed += 1
        except Exception:
            failed += 1

    if not minute_parts:
        return BoardSeries(board=board, points=pd.Series(dtype=float), stock_count=total, failed_count=failed)

    combined = pd.concat(minute_parts, axis=1).fillna(0).sum(axis=1).sort_index().cumsum()
    return BoardSeries(board=board, points=combined, stock_count=total, failed_count=failed)


def make_wide_table(series_list: Iterable[BoardSeries]) -> pd.DataFrame:
    rows = []
    for item in series_list:
        if item.points.empty:
            continue
        row = {"板块": item.board, "样本股数": item.stock_count, "失败数": item.failed_count}
        for ts_value, value in item.points.items():
            row[pd.Timestamp(ts_value).strftime("%Y-%m-%d %H:%M")] = round(float(value), 4)
        rows.append(row)
    return pd.DataFrame(rows)


def adjust_label_positions(values: list[float], min_gap: float) -> list[float]:
    if not values:
        return []
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    adjusted = [0.0] * len(values)
    prev = -float("inf")
    for idx, val in indexed:
        y = max(float(val), prev + min_gap)
        adjusted[idx] = y
        prev = y
    return adjusted


def trading_axis_value(ts_value: pd.Timestamp) -> float:
    ts_value = pd.Timestamp(ts_value)
    day = ts_value.normalize()
    minutes = int((ts_value - day).total_seconds() // 60)
    morning_start = 9 * 60 + 30
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60

    if minutes <= morning_end:
        return float(minutes - morning_start)
    if minutes >= afternoon_start:
        return float(120 + SNAPSHOT_INTERVAL_MINUTES + minutes - afternoon_start)
    return 120.0


def format_tick_label(ts_value: pd.Timestamp, show_date: bool) -> str:
    fmt = "%m-%d %H:%M" if show_date else "%H:%M"
    return pd.Timestamp(ts_value).strftime(fmt)


def figure_png_bytes(fig: plt.Figure) -> bytes:
    png_buffer = BytesIO()
    fig.savefig(png_buffer, format="png", bbox_inches="tight", dpi=180)
    return png_buffer.getvalue()


def render_png_download_button(png_bytes: bytes, file_name: str) -> None:
    encoded_png = base64.b64encode(png_bytes).decode("ascii")
    safe_file_name = html.escape(file_name, quote=True)
    st.markdown(
        f"""
        <style>
        .download-png-button {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 120px;
            height: 42px;
            padding: 0 18px;
            border-radius: 6px;
            background: #ff4b4b;
            color: #ffffff !important;
            font-weight: 700;
            text-decoration: none !important;
        }}
        .download-png-button:hover {{
            background: #e63b3b;
            color: #ffffff !important;
            text-decoration: none !important;
        }}
        </style>
        <a class="download-png-button"
           href="data:image/png;base64,{encoded_png}"
           download="{safe_file_name}">下载PNG</a>
        """,
        unsafe_allow_html=True,
    )


def plot_board_flow(series_list: list[BoardSeries], title: str) -> plt.Figure:
    non_empty = [s for s in series_list if not s.points.empty]
    fig, ax = plt.subplots(figsize=(9, 14), dpi=150)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    try:
        plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    if not non_empty:
        ax.text(0.5, 0.5, "没有可绘制数据", ha="center", va="center", transform=ax.transAxes, fontsize=18)
        return fig

    final_values = [float(s.points.iloc[-1]) for s in non_empty]
    order = np.argsort(final_values)[::-1]
    top_rank_by_item = {int(item_idx): rank for rank, item_idx in enumerate(order[:5])}
    plotted = []

    highlighted = {int(idx) for idx in order[:5]}
    for color_idx, item_idx in enumerate(order):
        item = non_empty[int(item_idx)]
        x = np.array([trading_axis_value(ts_value) for ts_value in item.points.index], dtype=float)
        y = item.points.values.astype(float)
        is_highlighted = int(item_idx) in highlighted
        if is_highlighted:
            color = TOP_LINE_COLORS[top_rank_by_item[int(item_idx)] % len(TOP_LINE_COLORS)]
        else:
            color = BACKGROUND_LINE_COLORS[color_idx % len(BACKGROUND_LINE_COLORS)]
        ax.plot(
            x,
            y,
            linewidth=2.8 if is_highlighted else 0.9,
            color=color,
            alpha=0.98 if is_highlighted else 0.28,
        )
        plotted.append((item, color, float(y[-1]), float(x[-1])))

    all_values = np.concatenate([s.points.values.astype(float) for s in non_empty])
    y_min, y_max = float(np.nanmin(all_values)), float(np.nanmax(all_values))
    y_span = max(y_max - y_min, 1.0)
    ax.set_ylim(y_min - y_span * 0.08, y_max + y_span * 0.08)
    ax.margins(x=0.02)

    labeled = plotted[:5]
    label_values = [p[2] for p in labeled]
    label_positions = adjust_label_positions(label_values, y_span * 0.025)
    unique_times = sorted({pd.Timestamp(ts_value) for s in non_empty for ts_value in s.points.index})
    unique_x = [trading_axis_value(ts_value) for ts_value in unique_times]
    last_x = max(p[3] for p in plotted)
    label_x = last_x + 14
    x_pad = 55
    if len(unique_times) == 1:
        ax.set_xlim(unique_x[0] - 15, unique_x[0] + x_pad)
        tick_x = unique_x
        tick_times = unique_times
    else:
        ax.set_xlim(min(unique_x) - 4, last_x + x_pad)
        if len(unique_times) <= 18:
            tick_indices = list(range(len(unique_times)))
        else:
            tick_indices = sorted(set(np.linspace(0, len(unique_times) - 1, 18).round().astype(int)))
        tick_x = [unique_x[i] for i in tick_indices]
        tick_times = [unique_times[i] for i in tick_indices]
    show_date = len({ts_value.date() for ts_value in unique_times}) > 1
    ax.set_xticks(tick_x)
    ax.set_xticklabels([format_tick_label(ts_value, show_date) for ts_value in tick_times])

    for (item, color, final, last_time), label_y in zip(labeled, label_positions):
        ax.scatter([last_time], [final], color=[color], s=28, zorder=5)
        ax.plot([last_time, label_x - 2], [final, label_y], color=color, linewidth=0.9, alpha=0.55, zorder=4)
        display_final = 0.0 if abs(final) < 0.05 else final
        ax.text(
            label_x,
            label_y,
            f"{item.board}{display_final:.0f}",
            color=color,
            fontsize=12.5,
            fontweight="bold",
            va="center",
        )

    ax.set_title(title, loc="left", fontsize=24, fontweight="bold", pad=12)
    ax.axhline(0, color="#666", linewidth=0.9, alpha=0.7)
    ax.grid(True, linestyle="--", linewidth=0.65, alpha=0.35)
    for label in ax.get_xticklabels():
        label.set_rotation(48)
        label.set_horizontalalignment("right")
        label.set_fontsize(10)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda value, _: f"{value:.0f}"))
    ax.tick_params(axis="y", labelsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.text(
        0.03,
        0.985,
        "单位：亿元",
        transform=ax.transAxes,
        fontsize=11,
        color="#555555",
        va="top",
    )
    fig.tight_layout()
    return fig


def run_app() -> None:
    st.set_page_config(page_title="板块资金流", page_icon="📈", layout="wide")
    st.title("板块资金流")

    if not KB_PATH.exists():
        st.error(f"找不到映射文件：{KB_PATH}")
        st.stop()

    mapping = load_mapping(str(KB_PATH))
    board_counts = mapping.groupby("板块")["ts_code"].nunique().sort_values(ascending=False)
    boards = board_counts.index.tolist()

    with st.sidebar:
        st.header("参数")
        token_default = read_token_from_shell() or ""
        token = st.text_input("Tushare Token", value=token_default, type="password")
        ignore_proxy = True
        data_mode = st.radio(
            "数据模式",
            [
                "Tushare rt_k 全市场快照（推荐）",
                "Tushare Pro 实时分钟快照（受频控）",
            ],
            index=0,
        )
        lookback_days = 1
        run_all_boards = True
        use_all_stocks = True
        max_board_size = int(board_counts.max())
        if use_all_stocks:
            max_stocks = max_board_size
        else:
            max_stocks = st.slider(
                "每个板块最多取样股票数",
                min_value=1,
                max_value=min(120, max_board_size),
                value=min(8, max_board_size),
                step=1,
            )
        batch_size = 50
        workers = 8
        if data_mode == "Tushare rt_k 全市场快照（推荐）":
            pass
        elif data_mode == "全市场盘口快照（备用，Tushare 爬虫源）":
            batch_size = st.slider("每批股票数", min_value=10, max_value=50, value=50, step=10)
            workers = st.slider("并发批数", min_value=1, max_value=16, value=8, step=1)
        elif data_mode == "Tushare Pro 实时分钟快照（受频控）":
            batch_size = st.slider("每批股票数", min_value=100, max_value=1000, value=300, step=100)

        if run_all_boards:
            selected_boards = boards
        else:
            default_count = min(8, len(boards))
            selected_boards = st.multiselect(
                "选择板块",
                boards,
                default=boards[:default_count],
                format_func=lambda b: f"{b} ({int(board_counts.loc[b])}只)",
            )

        st.caption(f"{len(boards)} 个板块 / {mapping['ts_code'].nunique()} 只股票")

    snapshot_count = 0
    snapshot_stock_count = 0
    source_snapshot_count = 0
    current_source = source_for_mode(data_mode)
    if SNAPSHOT_PATH.exists():
        try:
            snapshot_meta = pd.read_csv(SNAPSHOT_PATH, usecols=lambda c: c in {"time", "ts_code", "source"})
            snapshot_meta["time"] = pd.to_datetime(snapshot_meta["time"], errors="coerce")
            snapshot_meta = filter_snapshot_nodes(snapshot_meta.dropna(subset=["time"]))
            snapshot_count = snapshot_meta["time"].nunique()
            snapshot_stock_count = snapshot_meta["ts_code"].nunique()
            if current_source and "source" in snapshot_meta.columns:
                source_snapshot_count = snapshot_meta.loc[snapshot_meta["source"].eq(current_source), "time"].nunique()
        except Exception:
            pass
    if current_source:
        st.caption(f"{current_source} 快照点：{source_snapshot_count} / 覆盖股票：{snapshot_stock_count}")
    else:
        st.caption(f"快照点：{snapshot_count} / 覆盖股票：{snapshot_stock_count}")

    if st.button("生成图", type="primary", width="stretch"):
        if not selected_boards:
            st.error("请至少选择一个板块。")
            st.stop()

        today = datetime.now().strftime("%Y%m%d")
        snapshot_modes = (
            "Tushare rt_k 全市场快照（推荐）",
            "全市场盘口快照（备用，Tushare 爬虫源）",
            "Tushare Pro 实时分钟快照（受频控）",
        )

        if data_mode in snapshot_modes:
            trade_dates = [today]
            scoped = selected_mapping(mapping, selected_boards, max_stocks)
            ts_codes = sorted(scoped["ts_code"].drop_duplicates().tolist())
            all_snapshots = load_snapshots(trade_dates)
            all_snapshots = all_snapshots[all_snapshots["ts_code"].isin(ts_codes)]
            active_source = source_for_mode(data_mode)
            if active_source and "source" in all_snapshots.columns:
                all_snapshots = all_snapshots[all_snapshots["source"].eq(active_source)]
            all_snapshots = filter_snapshot_nodes(all_snapshots)
            snapshot_times = sorted(all_snapshots["time"].dropna().dt.strftime("%H:%M").unique().tolist())
            if all_snapshots.empty:
                st.warning("没有快照数据。")
                st.caption(f"已有时间：{', '.join(snapshot_times) if snapshot_times else '无'}")
                st.stop()
            results = aggregate_boards_from_snapshots(
                mapping,
                all_snapshots,
                selected_boards,
                max_stocks,
                failed_codes=set(),
            )
        else:
            if not token:
                st.error("没有找到 Tushare Token。请在侧边栏填写，或设置 TUSHARE_TOKEN 环境变量。")
                st.stop()
            if ignore_proxy:
                disable_proxy_env()
            with st.spinner("正在获取交易日..."):
                try:
                    trade_dates = get_recent_trade_dates(token, today, lookback_days, ignore_proxy)
                except Exception as exc:
                    st.warning(f"交易日接口失败，先按今天运行：{exc}")
                    trade_dates = [today]
            progress = st.progress(0)
            status = st.empty()
            total_steps = max(1, sum(min(int(board_counts.loc[b]), max_stocks) for b in selected_boards))
            done_steps = 0

            def report(board: str, i: int, total: int, code: str) -> None:
                nonlocal done_steps
                done_steps += 1
                progress.progress(min(done_steps / total_steps, 1.0))
                status.write(f"正在获取 `{board}`：{i}/{total} `{code}`")

            results = []
            for board in selected_boards:
                result = aggregate_board(token, mapping, board, trade_dates, max_stocks, ignore_proxy, report)
                results.append(result)
            progress.empty()

        available = [r for r in results if not r.points.empty]
        if not available:
            st.error("没有可绘制数据。")
            st.stop()

        date_label = "、".join([f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in trade_dates])
        title = f"{date_label} 板块资金流"
        fig = plot_board_flow(available, title)
        st.pyplot(fig, clear_figure=False, width="stretch")

        png_bytes = figure_png_bytes(fig)
        png_path = APP_DIR / "latest_board_flow.png"
        png_path.write_bytes(png_bytes)
        render_png_download_button(png_bytes, f"board_flow_{datetime.now():%Y%m%d_%H%M}.png")

        summary = pd.DataFrame(
            [
                {
                    "板块": item.board,
                    "最终值_亿": round(float(item.points.iloc[-1]), 4),
                    "样本股数": item.stock_count,
                    "失败数": item.failed_count,
                }
                for item in available
            ]
        ).sort_values("最终值_亿", ascending=False)
        st.dataframe(summary, width="stretch")


if __name__ == "__main__":
    run_app()
