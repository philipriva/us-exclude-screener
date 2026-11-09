#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions / CLI: 唔考慮篩選 — 11 gates run separately → Sheets + combine.

Formulas match us_exclude_screener_app.pyw (defaults):
1. 日線 MCDX 籌碼 — SMA10(PC)<SMA10(LC)
2. 日線 close < EMA200
3. 日線 close < EMA50
4. 市值 < 100M (unknown → skip)
5. 日線 TD Sequential — S2 only
6. 週線 TD Sequential — S1+S2
7. 週線主力能量 — 做空 > 做多
8. 週線 KDJ 黃柱 — J crossunder 50
9. 月線 KDJ 黃柱 — latest month bar (incl. forming)
10. 月線 SAR 喺價上 — TV ta.sar; latest month bar (incl. forming)
11. 月線主力能量 — 做空 > 做多 (same Tongdaxin as weekly; incl. forming month)

Universe: Nasdaq Trader US incl. ETF/Fund. Yahoo auto_adjust=True.
Default: drop forming *daily* bar before 16:15 ET; weekly/monthly gates use the
latest bar even if incomplete (matches TradingView current candle).
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from us_session import (
    CalendarUnavailableError,
    last_complete_us_session_date,
    session_bar_date,
)

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
N, SMA, BATCH = 100, 10, 50
MCAP_MAX = 100_000_000
MCAP_WORKERS = 4
DEFAULT_TD_D = {"S2"}
DEFAULT_TD_W = {"S1", "S2"}
PERIOD = "5y"
# Bounded download recovery (not a permanent poller).
DOWNLOAD_MAX_ROUNDS = 4
DOWNLOAD_RETRY_SLEEP_BASE = 2.0
DOWNLOAD_THREADS = False  # lower Yahoo concurrency; rate-limit was silent under threads=True
# Do NOT blacklist real Nasdaq symbols such as "NA" (Nano Labs). Only blank tokens.

GATE_ORDER = [
    "01_mcdx",
    "02_ema200",
    "03_ema50",
    "04_mcap",
    "05_td_daily",
    "06_td_weekly",
    "07_zhuli",
    "08_kdj_weekly",
    "09_kdj_monthly",
    "10_sar_monthly",
    "11_zhuli_monthly",
]

GATE_LABELS = {
    "01_mcdx": "日線 MCDX 籌碼",
    "02_ema200": "日線 close < EMA200",
    "03_ema50": "日線 close < EMA50",
    "04_mcap": "市值 < 100M",
    "05_td_daily": "日線 TD Sequential",
    "06_td_weekly": "週線 TD Sequential",
    "07_zhuli": "週線主力能量",
    "08_kdj_weekly": "週線 KDJ 黃柱",
    "09_kdj_monthly": "月線 KDJ 黃柱",
    "10_sar_monthly": "月線 SAR 喺價上",
    "11_zhuli_monthly": "月線主力能量",
}

# Duplicate tab B-column join order (1 = leftmost / highest priority)
DUP_GATE_PRIORITY = [
    "月線主力能量",
    "週線主力能量",
    "月線 SAR 喺價上",
    "日線 MCDX 籌碼",
    "月線 KDJ 黃柱",
    "週線 KDJ 黃柱",
    "市值 < 100M",
    "日線 close < EMA200",
    "日線 close < EMA50",
    "週線 TD Sequential",
    "日線 TD Sequential",
]


class UniverseParseError(RuntimeError):
    """FAIL_CLOSED: Nasdaq Trader directory HTTP/schema/footer failure."""


def _dir(url: str) -> pd.DataFrame:
    """Fetch a Nasdaq Trader symbol directory. Fail-closed on HTTP/schema/footer issues.

    Uses keep_default_na=False so real ticker ``NA`` (Nano Labs) is not swallowed by
    pandas' default NA token list.
    """
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        raise UniverseParseError(f"FAIL_CLOSED: HTTP fetch failed for {url}: {e}") from e
    lines = resp.text.splitlines()
    footer = [l for l in lines if l.startswith("File Creation")]
    if not footer:
        raise UniverseParseError(
            f"FAIL_CLOSED: missing 'File Creation' footer from {url}"
        )
    body = [l for l in lines if l and not l.startswith("File Creation")]
    if len(body) < 2:
        raise UniverseParseError(
            f"FAIL_CLOSED: directory body too short from {url} (rows={len(body)})"
        )
    try:
        df = pd.read_csv(
            io.StringIO("\n".join(body)),
            sep="|",
            dtype=str,
            keep_default_na=False,
            na_filter=False,
        )
    except Exception as e:
        raise UniverseParseError(f"FAIL_CLOSED: CSV parse failed for {url}: {e}") from e
    if df.empty or len(df.columns) < 1:
        raise UniverseParseError(f"FAIL_CLOSED: empty/invalid schema from {url}")
    return df


def universe() -> list[str]:
    a = _dir(NASDAQ_LISTED)
    b = _dir(OTHER_LISTED)
    if "Symbol" not in a.columns:
        raise UniverseParseError(
            "FAIL_CLOSED: nasdaqlisted.txt missing Symbol column; "
            f"cols={list(a.columns)}"
        )
    if "ACT Symbol" not in b.columns:
        raise UniverseParseError(
            "FAIL_CLOSED: otherlisted.txt missing ACT Symbol column; "
            f"cols={list(b.columns)}"
        )
    a = a.rename(columns={"Symbol": "sym"})
    b = b.rename(columns={"ACT Symbol": "sym"})
    pieces = []
    for df in (a, b):
        use = [c for c in ("sym", "Test Issue") if c in df.columns]
        pieces.append(df[use])
    df = pd.concat(pieces, ignore_index=True)
    # Empty string only — do not drop real tickers that look like NA/NONE/NULL tokens.
    df["sym"] = df["sym"].astype(str).str.strip()
    df = df[df["sym"] != ""]
    if "Test Issue" in df.columns:
        ti = df["Test Issue"].astype(str).str.strip().str.upper()
        df = df[ti.isin(("", "N"))]
    df = df[~df.sym.str.contains(r"[\$\^]", regex=True, na=False)]
    syms = {s.upper().replace(".", "-") for s in df.sym}
    # Blank only. Preserve NA / NONE / NULL if they ever appear as real symbols.
    return sorted(s for s in syms if s)


def save_universe_provenance(out_dir: Path, urls: list[tuple[str, str]] | None = None) -> dict:
    """Best-effort raw Nasdaq bytes + sha256 for future provenance (non-blocking)."""
    import hashlib
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = urls or [
        ("nasdaqlisted.txt", NASDAQ_LISTED),
        ("otherlisted.txt", OTHER_LISTED),
    ]
    info = {}
    for name, url in urls:
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            raw = resp.content
            path = out_dir / name
            path.write_bytes(raw)
            info[name] = {
                "url": url,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "path": str(path.name),
            }
        except Exception as e:
            info[name] = {"url": url, "error": str(e)}
    (out_dir / "universe_provenance.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return info


def parse_syms(text: str) -> list[str]:
    out, seen = [], set()
    for p in text.replace(",", " ").split():
        s = p.strip().upper().replace(".", "-")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def ny_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow() - timedelta(hours=4)


def drop_forming(d: pd.DataFrame) -> pd.DataFrame:
    now = ny_now()
    last = pd.Timestamp(d.index[-1]).date()
    if last == now.date() and (now.hour, now.minute) < (16, 15):
        return d.iloc[:-1]
    return d


def drop_forming_weekly(w: pd.DataFrame) -> pd.DataFrame:
    """Legacy: drop incomplete W-FRI bar. Weekly gates no longer call this."""
    if w is None or w.empty:
        return w
    now = ny_now()
    last = pd.Timestamp(w.index[-1]).normalize()
    fri_close = datetime(last.year, last.month, last.day, 16, 15)
    if now < fri_close:
        return w.iloc[:-1]
    return w


def drop_forming_monthly(m: pd.DataFrame) -> pd.DataFrame:
    """Legacy: drop incomplete month bar. Monthly gates no longer call this."""
    if m is None or m.empty:
        return m
    now = ny_now()
    last = pd.Timestamp(m.index[-1])
    month_end = (last + pd.offsets.MonthEnd(0)).date()
    if now.date() < month_end:
        return m.iloc[:-1] if len(m) > 1 else m
    if now.date() == month_end and (now.hour, now.minute) < (16, 15):
        return m.iloc[:-1] if len(m) > 1 else m
    return m


def to_weekly(d: pd.DataFrame) -> pd.DataFrame:
    cols = {}
    for name, how in (
        ("Open", "first"),
        ("High", "max"),
        ("Low", "min"),
        ("Close", "last"),
        ("Volume", "sum"),
    ):
        if name in d.columns:
            cols[name] = how
    if "Close" not in cols:
        return pd.DataFrame()
    return d.resample("W-FRI").agg(cols).dropna(subset=["Close"])


def to_monthly(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x.index = pd.to_datetime(x.index)
    cols = {}
    for name, how in (
        ("Open", "first"),
        ("High", "max"),
        ("Low", "min"),
        ("Close", "last"),
        ("Volume", "sum"),
    ):
        if name in x.columns:
            cols[name] = how
    if "Close" not in cols:
        return pd.DataFrame()
    need = ["Close"]
    if "High" in cols and "Low" in cols:
        need = ["Close", "High", "Low"]
    return x.resample("ME").agg(cols).dropna(subset=need)


def sma_cn(src: pd.Series, length: int) -> pd.Series:
    vals = src.astype(float).tolist()
    out: list[float] = []
    y = float("nan")
    for v in vals:
        if pd.isna(v):
            out.append(float("nan"))
            continue
        if pd.isna(y):
            y = float(v)
        else:
            y = (float(v) + (length - 1) * y) / length
        out.append(y)
    return pd.Series(out, index=src.index)


def kdj_j(df: pd.DataFrame) -> pd.Series:
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    c = df["Close"].astype(float)
    llv = l.rolling(9, min_periods=9).min()
    hhv = h.rolling(9, min_periods=9).max()
    rng = hhv - llv
    rsv = ((c - llv) / rng.replace(0, pd.NA) * 100).fillna(0)
    k = sma_cn(rsv, 3)
    d = sma_cn(k, 3)
    return 3 * k - 2 * d


def kdj_yellow(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 15:
        return False
    j = kdj_j(df)
    if len(j) < 2:
        return False
    prev, curr = j.iloc[-2], j.iloc[-1]
    if pd.isna(prev) or pd.isna(curr):
        return False
    return float(prev) >= 50.0 and float(curr) < 50.0


def parabolic_sar(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    start: float = 0.02,
    increment: float = 0.02,
    maximum: float = 0.20,
) -> pd.Series:
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    n = len(h)
    sar = np.full(n, np.nan)
    if n < 2:
        return pd.Series(sar, index=high.index, name="SAR")
    up = bool(c[1] >= c[0])
    af = float(start)
    if up:
        sar[1] = l[0]
        ep = float(h[1])
    else:
        sar[1] = h[0]
        ep = float(l[1])
    for i in range(2, n):
        cur = float(sar[i - 1] + af * (ep - sar[i - 1]))
        reversed_ = False
        if up:
            if l[i] < cur:
                up = False
                cur = ep
                ep = float(l[i])
                af = float(start)
                reversed_ = True
        else:
            if h[i] > cur:
                up = True
                cur = ep
                ep = float(h[i])
                af = float(start)
                reversed_ = True
        if not reversed_:
            if up:
                cur = min(cur, float(l[i - 1]), float(l[i - 2]))
                if h[i] > ep:
                    ep = float(h[i])
                    af = min(af + float(increment), float(maximum))
            else:
                cur = max(cur, float(h[i - 1]), float(h[i - 2]))
                if l[i] < ep:
                    ep = float(l[i])
                    af = min(af + float(increment), float(maximum))
        sar[i] = cur
    return pd.Series(sar, index=high.index, name="SAR")


def monthly_sar_above_close(m: pd.DataFrame) -> bool:
    if m is None or len(m) < 3:
        return False
    try:
        c, h, l = _ohlc(m)
    except KeyError:
        return False
    sar = parabolic_sar(h, l, c)
    if pd.isna(sar.iloc[-1]) or pd.isna(c.iloc[-1]):
        return False
    return float(c.iloc[-1]) < float(sar.iloc[-1])


def zhuli_bear_gt_bull(w: pd.DataFrame) -> bool:
    if w is None or w.empty or len(w) < 40:
        return False
    try:
        c, h, l = _ohlc(w)
    except KeyError:
        return False
    hh36 = h.rolling(36, min_periods=36).max()
    ll36 = l.rolling(36, min_periods=36).min()
    rng36 = hh36 - ll36
    var1 = (hh36 - c) / rng36 * 100.0
    # Keep NaN while HHV/LLV not ready; .where(rng>0,0) treats NaN as 0 and poisons SMA_CN
    var1 = var1.mask(rng36.eq(0) & rng36.notna(), 0.0)
    bear = sma_cn(var1, 3)
    hh9 = h.rolling(9, min_periods=9).max()
    ll9 = l.rolling(9, min_periods=9).min()
    rng9 = hh9 - ll9
    var2 = (c - ll9) / rng9 * 100.0
    var2 = var2.mask(rng9.eq(0) & rng9.notna(), 0.0)
    bull = sma_cn(var2, 5) - 8.0
    b, u = float(bear.iloc[-1]), float(bull.iloc[-1])
    if pd.isna(b) or pd.isna(u):
        return False
    return b > u


def fmcdx(price, h, l, n=N):
    lo = l.rolling(n, min_periods=n).min()
    hi = h.rolling(n, min_periods=n).max()
    rng = hi - lo
    step = (rng / 100.0).where(rng > 0, 1e-8)
    return ((price - lo) / step).clip(0, 100)


def _ohlc(d):
    if isinstance(d.columns, pd.MultiIndex):
        d = d.copy()
        d.columns = d.columns.get_level_values(0)
    cols = {str(c).lower(): c for c in d.columns}
    for need in ("close", "high", "low"):
        if need not in cols:
            raise KeyError(need)
    return (
        pd.to_numeric(d[cols["close"]], errors="coerce"),
        pd.to_numeric(d[cols["high"]], errors="coerce"),
        pd.to_numeric(d[cols["low"]], errors="coerce"),
    )


def td_setup_counts(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Return (red/S count, green/B count). S=RED=close<close[4]."""
    c = close.astype(float)
    b_count = pd.Series(0, index=c.index, dtype=int)
    s_count = pd.Series(0, index=c.index, dtype=int)
    bc = sc = 0
    vals = c.tolist()
    for i in range(len(vals)):
        if i < 4 or pd.isna(vals[i]) or pd.isna(vals[i - 4]):
            bc = 0
            sc = 0
        else:
            bc = bc + 1 if vals[i] < vals[i - 4] else 0
            sc = sc + 1 if vals[i] > vals[i - 4] else 0
        b_count.iloc[i] = bc
        s_count.iloc[i] = sc
    return b_count, s_count


def td_match(df: pd.DataFrame, codes: set[str]) -> bool:
    if not codes or df is None or df.empty or "Close" not in df.columns:
        return False
    red_count, green_count = td_setup_counts(df["Close"])
    red, green = int(red_count.iloc[-1]), int(green_count.iloc[-1])
    for code in codes:
        if not code or len(code) < 2:
            continue
        side, num_s = code[0].upper(), code[1:]
        try:
            num = int(num_s)
        except ValueError:
            continue
        if side == "S" and red == num:
            return True
        if side == "B" and green == num:
            return True
    return False


def fetch_mcap(sym: str) -> float | None:
    try:
        t = yf.Ticker(sym)
        for getter in (
            lambda: getattr(t.fast_info, "market_cap", None),
            lambda: (t.info or {}).get("marketCap"),
        ):
            try:
                v = getter()
                if v is not None and float(v) > 0:
                    return float(v)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _frame_has_ohlc(d: pd.DataFrame) -> bool:
    need = [c for c in ("Open", "High", "Low", "Close") if c in getattr(d, "columns", [])]
    return len(need) == 4


def _normalize_ohlc_frame(d: pd.DataFrame) -> pd.DataFrame | None:
    """Return OHLC frame with non-all-NaN rows, or None if unusable."""
    if d is None or getattr(d, "empty", True):
        return None
    if isinstance(d.columns, pd.MultiIndex):
        # Prefer field names on the last level; fall back to first if needed.
        lvl_last = [str(x) for x in d.columns.get_level_values(-1)]
        lvl_first = [str(x) for x in d.columns.get_level_values(0)]
        if set(("Open", "High", "Low", "Close")).issubset(set(lvl_last)):
            d = d.copy()
            d.columns = d.columns.get_level_values(-1)
        elif set(("Open", "High", "Low", "Close")).issubset(set(lvl_first)):
            d = d.copy()
            d.columns = d.columns.get_level_values(0)
        else:
            return None
    if not _frame_has_ohlc(d):
        return None
    # Drop rows where OHLC are all NaN; require Close/High/Low present for signals.
    d = d.dropna(subset=["Open", "High", "Low", "Close"], how="all")
    d = d.dropna(subset=["Close", "High", "Low"])
    if d.empty:
        return None
    # Reject frames whose entire OHLC columns are still NaN after dropna edge cases.
    if d[["Open", "High", "Low", "Close"]].isna().all().all():
        return None
    return d


def _extract_batch_frames(raw: pd.DataFrame | None, batch: list[str]) -> dict[str, pd.DataFrame]:
    """Pull per-ticker OHLC frames from a yfinance batch result (no raise → empty).

    Handles both MultiIndex level orders: (ticker, field) and (field, ticker),
    plus single-ticker flat columns.
    """
    got: dict[str, pd.DataFrame] = {}
    if raw is None or getattr(raw, "empty", True):
        return got
    multi = isinstance(raw.columns, pd.MultiIndex)
    for s in batch:
        try:
            d = None
            if multi:
                levels0 = set(map(str, raw.columns.get_level_values(0)))
                levels1 = set(map(str, raw.columns.get_level_values(-1)))
                if s in levels0 and set(("Open", "High", "Low", "Close")).issubset(levels1):
                    # (ticker, field)
                    d = raw[s]
                elif s in levels1 and set(("Open", "High", "Low", "Close")).issubset(levels0):
                    # (field, ticker)
                    d = raw.xs(s, axis=1, level=-1)
                elif s in levels0:
                    d = raw[s]
                elif s in levels1:
                    d = raw.xs(s, axis=1, level=-1)
            else:
                d = raw if len(batch) == 1 else None
                if d is None and s in raw.columns:
                    # unexpected flat layout — skip
                    d = None
            norm = _normalize_ohlc_frame(d if d is not None else raw if len(batch) == 1 and not multi else None)
            if norm is not None:
                got[s] = norm
        except Exception:
            continue
    return got


def _classify_download_failure(
    *,
    raised: Exception | None,
    stderr_text: str,
) -> str:
    """Honest failure class — do not label every empty as permanent delist or rate-limit."""
    blob = stderr_text or ""
    if raised is not None:
        blob = f"{blob}\n{type(raised).__name__}: {raised}"
    low = blob.lower()
    if (
        "yfratelimiterror" in low
        or "too many requests" in low
        or "rate limited" in low
        or "rate limit" in low
        or "http 429" in low
        or "429" in low and "rate" in low
    ):
        return "rate_limit_confirmed"
    if raised is not None:
        return f"exception:{type(raised).__name__}"
    if blob.strip():
        return "unavailable_or_unknown_with_stderr"
    return "unavailable_or_unknown_empty"


def download(
    syms: list[str],
    *,
    max_rounds: int = DOWNLOAD_MAX_ROUNDS,
    batch_size: int = BATCH,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Download daily bars with bounded serial retries for empty / rate-limited tickers.

    yfinance often *prints* ``YFRateLimitError`` / ``Failed downloads`` and returns a
    partial frame **without raising**. Empty / missing tickers are retried up to
    ``max_rounds``; failure reasons are classified honestly (confirmed rate-limit vs
    unavailable/UNKNOWN). Not a permanent poller.
    """
    import contextlib

    out: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    pending = list(syms)
    for round_i in range(max_rounds):
        if not pending:
            break
        print(
            f"download_round={round_i + 1}/{max_rounds} pending={len(pending)}",
            flush=True,
        )
        next_pending: list[str] = []
        n = len(pending)
        for i in range(0, n, batch_size):
            bt = pending[i : i + batch_size]
            print(
                f"download {min(i + batch_size, n)}/{n} (round {round_i + 1})",
                flush=True,
            )
            raw = None
            raised: Exception | None = None
            err_buf = io.StringIO()
            try:
                with contextlib.redirect_stderr(err_buf):
                    raw = yf.download(
                        bt,
                        period=PERIOD,
                        interval="1d",
                        group_by="ticker",
                        auto_adjust=True,
                        threads=DOWNLOAD_THREADS,
                        progress=False,
                    )
            except Exception as e:
                raised = e
                print(f"retryable_exception round={round_i + 1} err={e}", flush=True)
                raw = None
            stderr_text = err_buf.getvalue()
            got = _extract_batch_frames(raw, bt)
            for s in bt:
                if s in got:
                    out[s] = got[s]
                    errors.pop(s, None)
                else:
                    reason = _classify_download_failure(
                        raised=raised, stderr_text=stderr_text
                    )
                    errors[s] = reason
                    next_pending.append(s)
            time.sleep(0.25)
        pending = next_pending
        if pending and round_i < max_rounds - 1:
            sleep_s = DOWNLOAD_RETRY_SLEEP_BASE * (2 ** round_i)
            print(
                f"download_retry_sleep={sleep_s:.1f}s remaining={len(pending)}",
                flush=True,
            )
            time.sleep(sleep_s)
    if pending:
        # Summarize final classes honestly
        from collections import Counter
        c = Counter(errors[s] for s in pending if s in errors)
        print(
            f"download_still_missing={len(pending)} after {max_rounds} rounds "
            f"classes={dict(c)}",
            flush=True,
        )
    return out, errors


def classify_ticker_bars(
    data: dict[str, pd.DataFrame],
    errors: dict[str, str],
    universe: list[str],
    target_session,
    *,
    forming: bool = False,
) -> dict:
    """Per-ticker outcome. Freshness and history eligibility are reported separately.

    Short history still records last_bar_date so staleness is never masked.
    Does not invent a coverage PASS threshold.
    """
    target = target_session.isoformat() if hasattr(target_session, "isoformat") else str(target_session)
    per: dict[str, dict] = {}
    counts = {
        "universe": len(universe),
        "ok_target_session": 0,
        "stale": 0,
        "future": 0,
        "missing": 0,
        "error": 0,
        "no_bar_date": 0,
        "history_eligible": 0,
        "history_short": 0,
        "rate_limit_confirmed": 0,
        "unavailable_or_unknown": 0,
    }
    for s in universe:
        if s in data:
            d_raw = data[s]
            d_sig = d_raw if forming else drop_forming(d_raw)
            if d_sig is None or d_sig.empty:
                per[s] = {
                    "freshness": "no_bar_date",
                    "history_eligible": False,
                    "last_bar_date": None,
                    "bars": int(len(d_raw)),
                    "bars_signal": 0,
                    "note": "empty_after_drop_forming",
                }
                counts["no_bar_date"] += 1
                counts["history_short"] += 1
                continue
            try:
                last = session_bar_date(d_sig.index[-1])
                last_s = last.isoformat()
            except Exception:
                per[s] = {
                    "freshness": "no_bar_date",
                    "history_eligible": False,
                    "last_bar_date": None,
                    "bars": int(len(d_raw)),
                    "bars_signal": int(len(d_sig)),
                }
                counts["no_bar_date"] += 1
                counts["history_short"] += 1
                continue
            if last_s == target:
                freshness = "ok_target_session"
                counts["ok_target_session"] += 1
            elif last_s < target:
                freshness = "stale"
                counts["stale"] += 1
            else:
                freshness = "future"
                counts["future"] += 1
            hist_ok = bool(len(d_sig) >= 5)
            if hist_ok:
                counts["history_eligible"] += 1
            else:
                counts["history_short"] += 1
            per[s] = {
                "freshness": freshness,
                "history_eligible": hist_ok,
                "last_bar_date": last_s,
                "bars": int(len(d_raw)),
                "bars_signal": int(len(d_sig)),
            }
        else:
            reason = errors.get(s, "unavailable_or_unknown_empty")
            if reason == "rate_limit_confirmed":
                counts["rate_limit_confirmed"] += 1
                freshness = "missing"
                counts["missing"] += 1
            elif str(reason).startswith("exception:"):
                freshness = "error"
                counts["error"] += 1
            else:
                counts["unavailable_or_unknown"] += 1
                freshness = "missing"
                counts["missing"] += 1
            per[s] = {
                "freshness": freshness,
                "history_eligible": False,
                "last_bar_date": None,
                "error": reason,
            }
    return {
        "target_session": target,
        "counts": counts,
        "per_ticker": per,
        "source_input_count": len(universe),
        "downloaded_count": len(data),
    }


def selected_symbols(gate_hits: dict[str, list[str]]) -> list[str]:
    out, seen = [], set()
    for syms in gate_hits.values():
        for s in syms:
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def validate_selected_tickers(
    gate_hits: dict[str, list[str]],
    per_ticker: dict,
    target_session: str,
) -> list[dict]:
    """Every written/selected ticker must have real target_session OHLC + min history.

    No policy accepts selected stale/future/unknown. Returns list of violations.
    """
    bad: list[dict] = []
    for s in selected_symbols(gate_hits):
        info = per_ticker.get(s) or {}
        freshness = info.get("freshness")
        last = info.get("last_bar_date")
        hist = bool(info.get("history_eligible"))
        if freshness != "ok_target_session" or last != target_session:
            bad.append(
                {
                    "symbol": s,
                    "reason": "selected_not_target_session",
                    "freshness": freshness,
                    "last_bar_date": last,
                    "expected": target_session,
                }
            )
        elif not hist:
            bad.append(
                {
                    "symbol": s,
                    "reason": "selected_short_history",
                    "freshness": freshness,
                    "last_bar_date": last,
                    "bars_signal": info.get("bars_signal"),
                }
            )
    return bad


def eval_gates(d: pd.DataFrame, forming: bool = False) -> dict[str, bool]:
    """Evaluate price gates 1–3,5–11 (not mcap). Keys = GATE_ORDER without 04_mcap."""
    hits = {k: False for k in GATE_ORDER if k != "04_mcap"}
    if d is None or d.empty:
        return hits
    dd = d if forming else drop_forming(d)
    if dd is None or dd.empty or len(dd) < 5:
        return hits
    try:
        c, h, l = _ohlc(dd)
    except KeyError:
        return hits
    close = float(c.iloc[-1])
    if pd.isna(close):
        return hits

    # 1 MCDX
    if len(dd) >= N + SMA + 5:
        pc = fmcdx(c * 0.96, h, l)
        lc = 100.0 - fmcdx(c * 1.04, h, l)
        sp = float(pc.rolling(SMA, min_periods=SMA).mean().iloc[-1])
        sl = float(lc.rolling(SMA, min_periods=SMA).mean().iloc[-1])
        if not pd.isna(sp) and not pd.isna(sl) and sp < sl:
            hits["01_mcdx"] = True

    # 2 EMA200
    if len(dd) >= 200:
        e200 = float(c.ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1])
        if not pd.isna(e200) and close < e200:
            hits["02_ema200"] = True

    # 3 EMA50
    if len(dd) >= 50:
        e50 = float(c.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1])
        if not pd.isna(e50) and close < e50:
            hits["03_ema50"] = True

    # 5 daily TD S2
    if len(dd) >= 5 and td_match(pd.DataFrame({"Close": c}), DEFAULT_TD_D):
        hits["05_td_daily"] = True

    # weekly gates 6–8 — keep forming week (TV current candle)
    w = to_weekly(dd)
    if w is not None and not w.empty and len(w) >= 5:
        if td_match(w, DEFAULT_TD_W):
            hits["06_td_weekly"] = True
        if zhuli_bear_gt_bull(w):
            hits["07_zhuli"] = True
        if kdj_yellow(w):
            hits["08_kdj_weekly"] = True

    # monthly 9–11 — keep forming month (TV current candle)
    m_all = to_monthly(dd)
    if m_all is not None and not m_all.empty and kdj_yellow(m_all):
        hits["09_kdj_monthly"] = True
    if m_all is not None and not m_all.empty and len(m_all) >= 3 and monthly_sar_above_close(m_all):
        hits["10_sar_monthly"] = True
    if m_all is not None and not m_all.empty and zhuli_bear_gt_bull(m_all):
        hits["11_zhuli_monthly"] = True

    return hits


def write_local(gate_hits: dict[str, list[str]], out_dir: Path, asof: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    combine_rows: list[list[str]] = []
    for key in GATE_ORDER:
        syms = sorted(set(gate_hits.get(key, [])))
        label = GATE_LABELS[key]
        path = out_dir / f"exclude_{key}.txt"
        path.write_text("\n".join(syms) + ("\n" if syms else ""), encoding="utf-8")
        csv_path = out_dir / f"exclude_{key}.csv"
        csv_path.write_text(
            "symbol,gate\n" + "".join(f"{s},{label}\n" for s in syms),
            encoding="utf-8",
        )
        for s in syms:
            combine_rows.append([s, label])
        print(f"  {key}: {len(syms)} → {path.name}", flush=True)

    combine_rows.sort(key=lambda r: (r[0], r[1]))
    combine = out_dir / "us_exclude_screener_combine.csv"
    with combine.open("w", encoding="utf-8") as f:
        f.write("symbol,gate\n")
        for s, g in combine_rows:
            f.write(f"{s},{g}\n")
    meta = out_dir / "run_meta.txt"
    meta.write_text(
        f"asof_et={asof}\n"
        f"gates={len(GATE_ORDER)}\n"
        f"combine_rows={len(combine_rows)}\n"
        f"unique_symbols={len({r[0] for r in combine_rows})}\n",
        encoding="utf-8",
    )
    print(f"combine rows={len(combine_rows)} → {combine}", flush=True)
    return combine


class SheetsConfigError(RuntimeError):
    """Production Sheets path cannot proceed (SA / import / config)."""


def write_failure_receipt(out_dir: Path, **fields) -> Path:
    """Persist a failure sheets_receipt.json even when writes never started."""
    from sheets_writer import _atomic_write_json

    receipt = {
        "status": fields.get("status", "failed"),
        "sheets_write_attempted": bool(fields.get("sheets_write_attempted", False)),
        "sheets_write_success": False,
        "ready_for_sheets_prewrite": fields.get("ready_for_sheets_prewrite"),
        "errors": list(fields.get("errors") or []),
        "targets": fields.get("targets") or {},
    }
    path = out_dir / "sheets_receipt.json"
    _atomic_write_json(path, receipt)
    return path


def maybe_write_sheets(
    gate_hits: dict[str, list[str]],
    sheet_ids_path: Path,
    *,
    out_dir: Path,
    ready_for_sheets_prewrite: bool,
) -> dict:
    """Production Sheets write path — never silent-skip.

    Requires GOOGLE_SERVICE_ACCOUNT_JSON, importable sheets_writer, and a complete
    13-target config. Returns the sheets_receipt dict. Raises on any failure.
    """
    import os
    from sheets_writer import (
        SheetsWriteError,
        list_targets_from_config,
        preflight_sheet_targets,
        write_all_gates,
    )

    receipt_path = out_dir / "sheets_receipt.json"
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        cfg0 = {"gates": {}, "combine": {}}
        if sheet_ids_path.exists():
            try:
                cfg0 = json.loads(sheet_ids_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        write_failure_receipt(
            out_dir,
            status="failed_missing_sa",
            sheets_write_attempted=False,
            ready_for_sheets_prewrite=ready_for_sheets_prewrite,
            errors=["GOOGLE_SERVICE_ACCOUNT_JSON empty — production path cannot skip"],
            targets={
                t["key"]: {
                    "status": "not_attempted",
                    "spreadsheet_id": t.get("spreadsheet_id"),
                    "tab": t.get("tab"),
                    "error": "missing SA",
                }
                for t in list_targets_from_config(cfg0, GATE_ORDER)
            },
        )
        raise SheetsConfigError(
            "FAIL_CLOSED: GOOGLE_SERVICE_ACCOUNT_JSON required when not --skip-sheets"
        )

    try:
        cfg = json.loads(sheet_ids_path.read_text(encoding="utf-8"))
    except Exception as e:
        write_failure_receipt(
            out_dir,
            status="failed_config",
            sheets_write_attempted=False,
            ready_for_sheets_prewrite=ready_for_sheets_prewrite,
            errors=[f"sheet_ids load failed: {e}"],
        )
        raise SheetsConfigError(f"FAIL_CLOSED: cannot load sheet_ids: {e}") from e

    try:
        preflight_sheet_targets(cfg, GATE_ORDER)
    except Exception as e:
        write_failure_receipt(
            out_dir,
            status="failed_preflight",
            sheets_write_attempted=False,
            ready_for_sheets_prewrite=ready_for_sheets_prewrite,
            errors=[str(e)],
            targets={
                t["key"]: {
                    "status": "not_attempted",
                    "spreadsheet_id": t.get("spreadsheet_id"),
                    "tab": t.get("tab"),
                    "error": str(e),
                }
                for t in list_targets_from_config(cfg, GATE_ORDER)
            },
        )
        raise

    try:
        receipt = write_all_gates(
            sa_json,
            cfg,
            gate_hits,
            GATE_LABELS,
            GATE_ORDER,
            dup_priority=DUP_GATE_PRIORITY,
            receipt_path=receipt_path,
        )
    except SheetsWriteError:
        raise
    except Exception as e:
        write_failure_receipt(
            out_dir,
            status="failed_unexpected",
            sheets_write_attempted=True,
            ready_for_sheets_prewrite=ready_for_sheets_prewrite,
            errors=[str(e)],
        )
        raise SheetsWriteError(f"FAIL_CLOSED: {e}") from e

    receipt["ready_for_sheets_prewrite"] = ready_for_sheets_prewrite
    from sheets_writer import _atomic_write_json

    _atomic_write_json(receipt_path, receipt)
    return receipt


def main() -> int:
    ap = argparse.ArgumentParser(description="US exclude screener — 11 separate gates")
    ap.add_argument("--symbols", default="", help="Comma/space symbols; empty=full universe")
    ap.add_argument("--forming", action="store_true", help="Include forming daily bar")
    ap.add_argument("--out-dir", default="out", help="Local output directory")
    ap.add_argument("--sheet-ids", default="sheet_ids.json", help="Sheet ID config")
    ap.add_argument("--skip-sheets", action="store_true", help="Never write Google Sheets")
    ap.add_argument("--gates", default="", help="Comma gate keys to run (default=all)")
    args = ap.parse_args()

    if args.symbols.strip():
        syms = parse_syms(args.symbols)
    else:
        print("universe...", flush=True)
        try:
            syms = universe()
        except UniverseParseError as e:
            print(str(e), flush=True)
            return 3
    print(f"universe={len(syms)}", flush=True)

    want = set(GATE_ORDER)
    if args.gates.strip():
        want = {g.strip() for g in args.gates.replace(",", " ").split() if g.strip()}
        unknown = want - set(GATE_ORDER)
        if unknown:
            print("unknown gates:", unknown, flush=True)
            return 2

    try:
        target_session = last_complete_us_session_date()
    except CalendarUnavailableError as e:
        print(f"FAIL_CLOSED calendar: {e}", flush=True)
        return 3
    print(f"target_session={target_session.isoformat()}", flush=True)

    try:
        data, dl_errors = download(syms)
    except Exception as e:
        print(f"FAIL_CLOSED download: {e}", flush=True)
        return 4
    print(f"data={len(data)} missing={len(dl_errors)}", flush=True)
    outcome = classify_ticker_bars(
        data, dl_errors, syms, target_session, forming=args.forming
    )
    print(f"bar_counts={json.dumps(outcome['counts'])}", flush=True)
    target_iso = target_session.isoformat()

    # Only evaluate gates on target-session-fresh series (reject stale/future/unknown).
    gate_hits: dict[str, list[str]] = {k: [] for k in GATE_ORDER}
    scan_syms = [
        s for s, info in outcome["per_ticker"].items()
        if info.get("freshness") == "ok_target_session" and s in data
    ]
    print(f"scan_eligible_target_session={len(scan_syms)}", flush=True)
    for i, s in enumerate(scan_syms):
        hits = eval_gates(data[s], forming=args.forming)
        for k, v in hits.items():
            if v and k in want:
                gate_hits[k].append(s)
        if i % 500 == 0:
            print(f"scan {i}/{len(scan_syms)}", flush=True)

    if "04_mcap" in want:
        print(f"mcap check {len(scan_syms)}...", flush=True)
        with ThreadPoolExecutor(max_workers=MCAP_WORKERS) as ex:
            futs = {ex.submit(fetch_mcap, s): s for s in scan_syms}
            done = 0
            for fut in as_completed(futs):
                s = futs[fut]
                done += 1
                if done % 200 == 0:
                    print(f"mcap {done}/{len(scan_syms)}", flush=True)
                try:
                    cap = fut.result()
                except Exception:
                    cap = None
                if cap is not None and cap < MCAP_MAX:
                    gate_hits["04_mcap"].append(s)

    for k in list(gate_hits):
        if k not in want:
            gate_hits[k] = []

    selected_bad = validate_selected_tickers(
        gate_hits, outcome["per_ticker"], target_iso
    )
    if selected_bad:
        print(
            f"FAIL_CLOSED selected_date_guard violations={len(selected_bad)} "
            f"sample={selected_bad[:5]}",
            flush=True,
        )

    asof = ny_now().strftime("%Y-%m-%d %H:%M ET")
    out_dir = Path(args.out_dir)
    write_local(gate_hits, out_dir, asof)
    universe_prov = save_universe_provenance(out_dir)

    sheet_target_plan = {
        "policy": "owner_readme_sheet_ids",
        "combine_and_duplicate": {
            "spreadsheet_id": None,
            "tabs": ["combine", "Duplicate"],
        },
        "gates": {
            "mode": "separate_spreadsheets_per_gate",
            "note": (
                "Owner contract: 11 independent gate spreadsheets in GROK/github; "
                "combine+Duplicate only on main us_exclude_screener. "
                "Do not also write exclude_* tabs onto the combine workbook."
            ),
        },
    }
    try:
        cfg = json.loads(Path(args.sheet_ids).read_text(encoding="utf-8"))
        sheet_target_plan["combine_and_duplicate"]["spreadsheet_id"] = (
            (cfg.get("combine") or {}).get("id")
        )
        sheet_target_plan["gates"]["ids"] = {
            k: (cfg.get("gates") or {}).get(k, {}).get("id") for k in GATE_ORDER
        }
    except Exception as e:
        sheet_target_plan["config_error"] = str(e)

    missing_n = int(outcome["counts"]["missing"]) + int(outcome["counts"]["error"])
    download_complete = missing_n == 0
    ok_target_n = int(outcome["counts"]["ok_target_session"])
    # No owner-approved universe coverage fraction. Do NOT invent ExtraScan 0.90
    # or a homemade 100%-fresh business PASS. FAIL_CLOSED rules:
    #  - incomplete download after bounded retries → hold
    #  - zero usable target-session bars → hold
    #  - any selected ticker not target-session / short history → hold
    block_reasons: list[str] = []
    if not download_complete:
        block_reasons.append(
            f"download_incomplete missing_or_error={missing_n} "
            f"(rate_limit_confirmed={outcome['counts']['rate_limit_confirmed']}, "
            f"unavailable_or_unknown={outcome['counts']['unavailable_or_unknown']})"
        )
    if ok_target_n <= 0:
        block_reasons.append("no_ok_target_session_bars")
    if selected_bad:
        block_reasons.append(f"selected_not_all_target_session n={len(selected_bad)}")

    ready_for_sheets = len(block_reasons) == 0
    run_outcome = {
        "asof_et_wallclock": asof,
        "target_session": target_iso,
        "download_complete": download_complete,
        "ready_for_sheets": ready_for_sheets,
        "block_reasons": block_reasons,
        "counts": outcome["counts"],
        "downloaded_count": outcome["downloaded_count"],
        "source_input_count": outcome["source_input_count"],
        "source_input_hash": __import__("hashlib").sha256(
            ("\n".join(syms)).encode()
        ).hexdigest(),
        "selected_count": len(selected_symbols(gate_hits)),
        "selected_violations": selected_bad[:50],
        "policy": {
            "batch": BATCH,
            "download_max_rounds": DOWNLOAD_MAX_ROUNDS,
            "download_threads": DOWNLOAD_THREADS,
            "coverage_threshold": None,
            "coverage_threshold_note": (
                "No owner-approved freshness/coverage fraction for us-exclude; "
                "ExtraScan 0.90 must not be applied here. "
                "FAIL_CLOSED: incomplete download, zero target-session bars, or "
                "any selected stale/future/unknown/short-history blocks Sheets."
            ),
            "selected_date_guard": (
                "every selected ticker must have freshness=ok_target_session, "
                "last_bar_date==target_session, history_eligible=True"
            ),
        },
        "sheet_target_plan": sheet_target_plan,
        "gate_hit_counts": {k: len(set(gate_hits.get(k, []))) for k in GATE_ORDER},
        "errors_sample": dict(list(dl_errors.items())[:50]),
        "errors_total": len(dl_errors),
        "error_class_counts": {
            "rate_limit_confirmed": outcome["counts"]["rate_limit_confirmed"],
            "unavailable_or_unknown": outcome["counts"]["unavailable_or_unknown"],
            "error": outcome["counts"]["error"],
        },
    }
    (out_dir / "run_outcome.json").write_text(
        json.dumps(run_outcome, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "ticker_outcomes.json").write_text(
        json.dumps(outcome["per_ticker"], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    meta_txt = out_dir / "run_meta.txt"
    meta_txt.write_text(
        meta_txt.read_text(encoding="utf-8")
        + f"target_session={target_iso}\n"
        + f"downloaded={len(data)}\n"
        + f"missing={missing_n}\n"
        + f"ok_target_session={ok_target_n}\n"
        + f"stale={outcome['counts']['stale']}\n"
        + f"download_complete={download_complete}\n"
        + f"ready_for_sheets={ready_for_sheets}\n"
        + f"block_reasons={';'.join(block_reasons) if block_reasons else 'none'}\n",
        encoding="utf-8",
    )

    sheets_write_success = False
    sheets_receipt_status = "not_attempted"
    if args.skip_sheets:
        sheets_receipt_status = "skipped_explicit"
        write_failure_receipt(
            out_dir,
            status="skipped_explicit",
            sheets_write_attempted=False,
            ready_for_sheets_prewrite=ready_for_sheets,
            errors=["--skip-sheets set (local/CI only; not a production success)"],
            targets={
                f"gate_{k}": {"status": "not_attempted", "error": "skip-sheets"}
                for k in GATE_ORDER
            }
            | {
                "combine": {"status": "not_attempted", "error": "skip-sheets"},
                "Duplicate": {"status": "not_attempted", "error": "skip-sheets"},
            },
        )
        print("Sheets: skipped (--skip-sheets)", flush=True)
    elif not ready_for_sheets:
        sheets_receipt_status = "blocked_prewrite"
        write_failure_receipt(
            out_dir,
            status="blocked_prewrite",
            sheets_write_attempted=False,
            ready_for_sheets_prewrite=False,
            errors=list(block_reasons),
        )
        print("Sheets: BLOCKED — " + "; ".join(block_reasons), flush=True)
    else:
        try:
            receipt = maybe_write_sheets(
                gate_hits,
                Path(args.sheet_ids),
                out_dir=out_dir,
                ready_for_sheets_prewrite=True,
            )
            sheets_write_success = bool(receipt.get("sheets_write_success"))
            sheets_receipt_status = receipt.get("status") or (
                "success" if sheets_write_success else "failed"
            )
        except Exception as e:
            sheets_receipt_status = "failed"
            sheets_write_success = False
            print(f"Sheets: FAIL_CLOSED {e}", flush=True)
            block_reasons.append(f"sheets_write_failed:{e}")

    # Distinguish pre-write readiness from actual successful write+readback.
    run_outcome["ready_for_sheets"] = ready_for_sheets  # pre-write gate only
    run_outcome["ready_for_sheets_prewrite"] = ready_for_sheets
    run_outcome["sheets_write_attempted"] = (not args.skip_sheets) and ready_for_sheets
    run_outcome["sheets_write_success"] = sheets_write_success
    run_outcome["sheets_receipt_status"] = sheets_receipt_status
    (out_dir / "run_outcome.json").write_text(
        json.dumps(run_outcome, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for k in GATE_ORDER:
        print(f"COUNT {k}={len(set(gate_hits[k]))} ({GATE_LABELS[k]})", flush=True)

    if block_reasons or not (
        args.skip_sheets or sheets_write_success
    ):
        # Production path without --skip-sheets must have sheets_write_success.
        if not args.skip_sheets and not sheets_write_success and not block_reasons:
            block_reasons.append("sheets_write_not_confirmed")
        print(
            "EXIT_FAIL_CLOSED " + "; ".join(block_reasons or ["sheets_incomplete"])
            + " (local artifacts/receipt written; workflow must not look green)",
            flush=True,
        )
        return 1
    # --skip-sheets local/CI: data gates passed; sheets intentionally not written.
    if args.skip_sheets:
        print(
            "EXIT_OK_SKIP_SHEETS (data gates passed; Sheets not written by request)",
            flush=True,
        )
    return 0

if __name__ == "__main__":
    sys.exit(main())
