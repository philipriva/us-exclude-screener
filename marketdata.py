#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared US daily price fetch + session calendar (stdlib-safe to import).

Keep this file in sync across ExtraScan, us-exclude-screener, my-stock-scanner,
and Q-scanner.

Yahoo is the primary source for every symbol. After the main yf.download
batch (proven params: interval=1d, auto_adjust=True, threads=True,
group_by=ticker, plus start=utcnow-lookback_days OR a caller period),
symbols that are missing or whose last complete bar != expected_session are
retried per-symbol / small-batch with period=5d and Ticker.history, then
Stooq is used ONLY to append the missing latest bar(s) onto existing Yahoo
history. Stooq never replaces Yahoo history.

Adjustment caveat: Stooq OHLC may be unadjusted or adjusted differently from
Yahoo auto_adjust=True. Gap-filled bars are therefore not guaranteed to be
on the same adjustment basis as the Yahoo prefix; they are used only so the
session date is present. Screening still prefers the Yahoo prefix.

Calendar functions use pandas_market_calendars (XNYS) when installed, else a
stdlib NYSE holiday / early-close fallback so skip_if_session_done.py can run
before pip install on GitHub Actions.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MIN_FRESH_FRAC = 0.90
LOOKBACK_DAYS = 1500
DEFAULT_BATCH = 100
DOWNLOAD_RETRIES = 3
DOWNLOAD_BACKOFF_SEC = 2.0
SYMBOL_RETRY_RETRIES = 1  # empty/delisted: do not triple-retry
STOOQ_TIMEOUT_SEC = 10.0
REQUEST_TIMEOUT_SEC = 10.0
YF_BATCH_TIMEOUT_SEC = 45.0
YF_RETRY_BUDGET_SEC = 360.0  # 5d batches + Ticker.history wall clock
STOOQ_BUDGET_SEC = 360.0
RETRY_CONCURRENCY = 8
MAX_HISTORY_SYMBOLS = 400
MAX_STOOQ_SYMBOLS = 400

# One-off full-session closures not produced by the standard NYSE rule set.
# 2025-01-09: National Day of Mourning (President Carter).
ONE_OFF_HOLIDAYS = frozenset({date(2025, 1, 9)})

__all__ = [
    "NY",
    "MIN_FRESH_FRAC",
    "LOOKBACK_DAYS",
    "DownloadStats",
    "ny_now",
    "is_nyse_trading_day",
    "session_close_datetime",
    "session_close_utc",
    "last_complete_us_session_date",
    "bar_date",
    "drop_forming",
    "last_bar_date",
    "freshness_ok",
    "format_status",
    "download_daily",
]


def ny_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(NY)
    if now.tzinfo is None:
        return now.replace(tzinfo=NY)
    return now.astimezone(NY)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian Easter (Western)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    if d.weekday() == 5:  # Saturday → Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday → Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    adj = (weekday - d.weekday()) % 7
    return d + timedelta(days=adj + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _stdlib_nyse_holidays(year: int) -> set[date]:
    hol = {
        _observed(date(year, 1, 1)),  # New Year
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial
        _observed(date(year, 7, 4)),  # Independence
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    if year >= 2022:
        hol.add(_observed(date(year, 6, 19)))  # Juneteenth
    hol |= {d for d in ONE_OFF_HOLIDAYS if d.year == year}
    return hol


def _stdlib_is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _stdlib_nyse_holidays(d.year)


def _stdlib_is_early_close(d: date) -> bool:
    if not _stdlib_is_trading_day(d):
        return False
    # Friday after Thanksgiving
    thanks = _nth_weekday(d.year, 11, 3, 4)
    if d == thanks + timedelta(days=1):
        return True
    # July 3 when it is a trading day
    if d.month == 7 and d.day == 3:
        return True
    # Christmas Eve when it is a trading day
    if d.month == 12 and d.day == 24:
        return True
    return False


# pandas_market_calendars get_calendar+schedule is expensive (~50–80ms).
# last_bar_date/drop_forming used to call this once per symbol, which burned
# ~7 min per freshness pass on a 6.6k universe and expired the 6 min retry
# budget before any 5d retry started (Zhuli 43345042225, 2026-11-13).
_MCAL_SCHED_CACHE: dict[date, object] = {}
_MCAL_UNAVAILABLE = object()
_SESSION_CLOSE_CACHE: dict[date, datetime] = {}


def _mcal_schedule(d: date):
    if d in _MCAL_SCHED_CACHE:
        cached = _MCAL_SCHED_CACHE[d]
        return None if cached is _MCAL_UNAVAILABLE else cached
    try:
        import pandas_market_calendars as mcal
    except Exception:
        _MCAL_SCHED_CACHE[d] = _MCAL_UNAVAILABLE
        return None
    try:
        nyse = mcal.get_calendar("XNYS")
        sched = nyse.schedule(start_date=d.isoformat(), end_date=d.isoformat())
        _MCAL_SCHED_CACHE[d] = sched
        return sched
    except Exception:
        _MCAL_SCHED_CACHE[d] = _MCAL_UNAVAILABLE
        return None


def is_nyse_trading_day(d: date) -> bool:
    sched = _mcal_schedule(d)
    if sched is not None:
        return len(sched) > 0
    return _stdlib_is_trading_day(d)


def session_close_datetime(session: date) -> datetime:
    """Official NYSE cash close in America/New_York (16:00, or 13:00 early close)."""
    cached = _SESSION_CLOSE_CACHE.get(session)
    if cached is not None:
        return cached
    sched = _mcal_schedule(session)
    if sched is not None and len(sched) > 0:
        close = sched.iloc[0]["market_close"]
        out = close.tz_convert(NY).to_pydatetime()
    else:
        hour, minute = (13, 0) if _stdlib_is_early_close(session) else (16, 0)
        out = datetime(session.year, session.month, session.day, hour, minute, tzinfo=NY)
    _SESSION_CLOSE_CACHE[session] = out
    return out


def session_close_utc(session: date) -> datetime:
    return session_close_datetime(session).astimezone(timezone.utc)


def last_complete_us_session_date(now: Optional[datetime] = None) -> date:
    """Most recently completed NYSE cash session (holidays + early closes)."""
    now = ny_now(now)
    d = now.date()
    for _ in range(15):
        if is_nyse_trading_day(d) and now >= session_close_datetime(d):
            return d
        d = d - timedelta(days=1)
    # Fail-safe: previous weekday
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d


def bar_date(ts) -> date:
    """Session calendar date of a daily bar index.

    tz-naive stamps are treated as already being the session date (Yahoo
    yf.download 1d). tz-aware stamps are converted to America/New_York first.
    """
    import pandas as pd

    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert(NY)
    return t.date()


def drop_forming(d, now: Optional[datetime] = None):
    """Drop today's daily bar until the NYSE cash session has closed."""
    if d is None:
        return d
    try:
        empty = d.empty
    except Exception:
        return d
    if empty:
        return d
    now = ny_now(now)
    last = bar_date(d.index[-1])
    if last == now.date() and now < session_close_datetime(now.date()):
        return d.iloc[:-1]
    return d


def last_bar_date(d, now: Optional[datetime] = None) -> Optional[date]:
    if d is None:
        return None
    try:
        if d.empty:
            return None
    except Exception:
        return None
    trimmed = drop_forming(d, now=now)
    if trimmed is None or trimmed.empty:
        return None
    return bar_date(trimmed.index[-1])


def freshness_ok(
    bar_dates: list[date],
    expected: date,
    min_frac: float = MIN_FRESH_FRAC,
    *,
    universe_n: Optional[int] = None,
) -> tuple[bool, dict]:
    """Freshness fraction.

    If universe_n is set, the denominator is the full requested universe
    (missing symbols count as not fresh) — CDEF style.
    Otherwise the denominator is len(bar_dates) (screened-only) — Daily style.
    """
    if universe_n is not None:
        n = universe_n
        fresh = sum(1 for x in bar_dates if x == expected)
    else:
        n = len(bar_dates)
        fresh = sum(1 for x in bar_dates if x == expected)
    if n <= 0:
        return False, {
            "screened": len(bar_dates),
            "universe": universe_n,
            "fresh": 0,
            "frac": 0.0,
            "expected": expected.isoformat(),
        }
    frac = fresh / n
    return frac >= min_frac, {
        "screened": len(bar_dates),
        "universe": universe_n if universe_n is not None else n,
        "fresh": fresh,
        "frac": round(frac, 4),
        "expected": expected.isoformat(),
        "unique_last_bars": sorted({x.isoformat() for x in bar_dates}),
    }


def format_status(
    *,
    session: date,
    writer: str,
    fresh: int,
    denom: int,
    yahoo: int,
    retry: int,
    stooq: int,
    result: str,
    extra: str = "",
) -> str:
    frac = (fresh / denom) if denom else 0.0
    parts = [
        f"STATUS session={session.isoformat()}",
        f"writer={writer}",
        f"fresh={fresh}/{denom}={frac:.4f}",
        f"yahoo={yahoo}",
        f"retry={retry}",
        f"stooq={stooq}",
        f"result={result}",
    ]
    if extra:
        parts.append(extra)
    return " ".join(parts)


@dataclass
class DownloadStats:
    requested: int = 0
    yahoo: int = 0
    retry: int = 0
    stooq: int = 0
    missing: int = 0
    stale_after: int = 0
    fresh: int = 0
    last_bar_hist: dict[str, int] = field(default_factory=dict)

    def freshness(self, expected: date, *, full_universe: bool, min_frac: float = MIN_FRESH_FRAC) -> tuple[bool, dict]:
        denom = self.requested if full_universe else (self.fresh + self.stale_after)
        ok, info = freshness_ok(
            [date.fromisoformat(d) for d, n in self.last_bar_hist.items() for _ in range(n)],
            expected,
            min_frac,
            universe_n=self.requested if full_universe else None,
        )
        # freshness_ok from hist works, but screened denom should use fresh+stale
        if not full_universe:
            n = self.fresh + self.stale_after
            frac = (self.fresh / n) if n else 0.0
            ok = n > 0 and frac >= min_frac
            info = {
                "screened": n,
                "universe": self.requested,
                "fresh": self.fresh,
                "frac": round(frac, 4),
                "expected": expected.isoformat(),
                "unique_last_bars": sorted(self.last_bar_hist),
            }
        else:
            info["unique_last_bars"] = sorted(self.last_bar_hist)
            info["fresh"] = self.fresh
            info["frac"] = round((self.fresh / self.requested) if self.requested else 0.0, 4)
            ok = self.requested > 0 and (self.fresh / self.requested) >= min_frac
        return ok, info


def _extract_batch_frames(raw, batch: list[str]) -> dict:
    import pandas as pd

    got: dict = {}
    if raw is None:
        return got
    try:
        if raw.empty:
            return got
    except Exception:
        return got
    for s in batch:
        try:
            d = raw[s] if len(batch) > 1 else raw
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(-1)
            d = d.dropna(how="all")
            if not d.empty:
                got[s] = d
        except Exception:
            pass
    return got


def _yf_download_once(tickers: list[str], *, period: Optional[str], lookback_days: int, threads: bool):
    import yfinance as yf
    from datetime import datetime as dt

    kwargs = dict(
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=threads,
        progress=False,
    )
    if period:
        return yf.download(tickers, period=period, **kwargs)
    start = (dt.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return yf.download(tickers, start=start, **kwargs)


def _merge_yahoo(long_df, short_df):
    """Prefer short (fresh Yahoo 5d / history) on overlapping dates; keep long prefix."""
    import pandas as pd

    if short_df is None or short_df.empty:
        return long_df
    if long_df is None or long_df.empty:
        return short_df
    a = long_df.copy()
    b = short_df.copy()
    if not isinstance(a.index, pd.DatetimeIndex):
        a.index = pd.to_datetime(a.index)
    if not isinstance(b.index, pd.DatetimeIndex):
        b.index = pd.to_datetime(b.index)
    # Align tz
    if a.index.tz is not None and b.index.tz is None:
        b.index = b.index.tz_localize(a.index.tz)
    elif b.index.tz is not None and a.index.tz is None:
        a.index = a.index.tz_localize(b.index.tz)
    elif a.index.tz is not None and b.index.tz is not None and a.index.tz != b.index.tz:
        b.index = b.index.tz_convert(a.index.tz)
    a = a[~a.index.isin(b.index)]
    out = pd.concat([a, b]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _stooq_symbol_candidates(sym: str) -> list[str]:
    s = sym.strip().lower()
    cands = [f"{s}.us", s.replace("-", "_") + ".us", s]
    # unique preserve order
    seen = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _fetch_stooq(sym: str):
    """Daily OHLCV from Stooq, or None.

    Returns a tz-naive Date index (session dates). May be unadjusted — see
    module docstring. Never used as a full Yahoo replacement.
    """
    import pandas as pd
    import requests

    last_err = None
    for code in _stooq_symbol_candidates(sym):
        url = f"https://stooq.com/q/d/l/?s={code}&i=d"
        try:
            resp = requests.get(url, timeout=STOOQ_TIMEOUT_SEC, headers={"User-Agent": "marketdata/1.0"})
            if resp.status_code != 200 or not resp.text or resp.text.startswith("No data"):
                continue
            # Stooq returns "No data" as body sometimes with 200
            if "Date" not in resp.text.splitlines()[0]:
                continue
            import io

            df = pd.read_csv(io.StringIO(resp.text))
            if df is None or df.empty or "Date" not in df.columns:
                continue
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.set_index("Date").sort_index()
            cols = {str(c).capitalize(): c for c in df.columns}
            need = {}
            for k in ("Open", "High", "Low", "Close"):
                src = cols.get(k)
                if src is None:
                    break
                need[k] = pd.to_numeric(df[src], errors="coerce")
            else:
                out = pd.DataFrame(need)
                if "Volume" in cols:
                    out["Volume"] = pd.to_numeric(df[cols["Volume"]], errors="coerce")
                out = out.dropna(subset=["High", "Low", "Close"])
                if not out.empty:
                    return out
        except Exception as exc:
            last_err = exc
            continue
    if last_err:
        print(f"  stooq {sym}: {last_err}", flush=True)
    return None


def _append_stooq_gaps(yahoo_df, stooq_df, expected: date):
    """Append Stooq bars strictly after the Yahoo last date, up to expected.

    Never overwrites or replaces Yahoo rows.
    """
    import pandas as pd

    if yahoo_df is None or yahoo_df.empty or stooq_df is None or stooq_df.empty:
        return yahoo_df, 0
    y = yahoo_df.copy()
    if not isinstance(y.index, pd.DatetimeIndex):
        y.index = pd.to_datetime(y.index)
    last_y = bar_date(y.index[-1])
    add_rows = []
    for ts, row in stooq_df.iterrows():
        d = bar_date(ts)
        if last_y < d <= expected:
            add_rows.append((d, row))
    if not add_rows:
        return y, 0
    # Build a frame with Yahoo columns
    recs = []
    for d, row in add_rows:
        rec = {}
        for c in y.columns:
            key = str(c)
            # case-insensitive match
            val = None
            if key in row.index:
                val = row[key]
            else:
                for cand in row.index:
                    if str(cand).lower() == key.lower():
                        val = row[cand]
                        break
            rec[c] = val
        recs.append(rec)
    add = pd.DataFrame(recs, index=pd.to_datetime([d for d, _ in add_rows]))
    if y.index.tz is not None:
        add.index = add.index.tz_localize(y.index.tz)
    out = pd.concat([y, add]).sort_index()
    # Keep first on duplicates — Yahoo wins
    out = out[~out.index.duplicated(keep="first")]
    return out, len(add_rows)



def _prioritize_pending(pending: list[str], out: dict) -> list[str]:
    """Retry stale-with-Yahoo-history first; missing/delisted last."""
    has, miss = [], []
    for s in pending:
        fr = out.get(s)
        if fr is not None and not getattr(fr, "empty", True):
            has.append(s)
        else:
            miss.append(s)
    return has + miss


def _budget_left(clock: Callable, start: float, budget: float) -> float:
    return budget - (clock() - start)


def _run_with_timeout(fn: Callable, timeout: float, *args, **kwargs):
    """Call fn; return None on timeout or exception.

    The worker thread may outlive the timeout (yfinance has no cancel);
    the caller moves on so a single hung request cannot stall the budget.
    """
    if timeout is None or timeout <= 0:
        return None
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(fn, *args, **kwargs)
        return fut.result(timeout=timeout)
    except Exception:
        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _needs_retry(frame, expected: date, now: Optional[datetime]) -> bool:
    lb = last_bar_date(frame, now=now)
    return lb is None or lb != expected


def download_daily(
    symbols: Iterable[str],
    expected_session: date,
    *,
    period: Optional[str] = None,
    lookback_days: int = LOOKBACK_DAYS,
    batch_size: int = DEFAULT_BATCH,
    pause_sec: float = 0.15,
    now: Optional[datetime] = None,
    yf_download: Optional[Callable] = None,
    yf_history: Optional[Callable] = None,
    stooq_fetch: Optional[Callable] = None,
    sleep: Callable = time.sleep,
    clock: Callable = time.monotonic,
    retry_budget_sec: float = YF_RETRY_BUDGET_SEC,
    stooq_budget_sec: float = STOOQ_BUDGET_SEC,
    request_timeout_sec: float = REQUEST_TIMEOUT_SEC,
    batch_timeout_sec: float = YF_BATCH_TIMEOUT_SEC,
    retry_concurrency: int = RETRY_CONCURRENCY,
) -> tuple[dict, DownloadStats]:
    """Fetch daily bars; Yahoo primary, Stooq only fills last-session gaps.

    yf_download / yf_history / stooq_fetch / sleep / clock are injectable for tests.

    After the main Yahoo batch, the 5d + Ticker.history retry phase is hard-capped
    at ``retry_budget_sec`` (default 6 min). The Stooq gap-fill phase is separately
    capped at ``stooq_budget_sec``. Per-request timeouts apply to Yahoo history
    and Stooq HTTP. When a budget is exhausted, leftover symbols stay as they
    are and the caller evaluates the .90 freshness gate on whatever was fetched.
    """
    syms = [str(s).strip() for s in symbols if str(s).strip()]
    stats = DownloadStats(requested=len(syms))
    out: dict = {}
    if not syms:
        return out, stats

    fetch = yf_download or (lambda tickers, **kw: _yf_download_once(tickers, **kw))
    hist_fn = yf_history
    stooq_fn = stooq_fetch if stooq_fetch is not None else _fetch_stooq

    def _fetch_batch(tickers, **kw):
        return _run_with_timeout(fetch, batch_timeout_sec, tickers, **kw)

    # --- 1) main Yahoo batch ---
    n = len(syms)
    for i in range(0, n, batch_size):
        bt = syms[i : i + batch_size]
        print(f"download {min(i + batch_size, n)}/{n}", flush=True)
        got = {}
        for attempt in range(DOWNLOAD_RETRIES):
            try:
                raw = _fetch_batch(bt, period=period, lookback_days=lookback_days, threads=True)
                if raw is None:
                    got = {}
                else:
                    got = _extract_batch_frames(raw, bt)
                if len(got) >= max(1, int(len(bt) * 0.5)):
                    break
                print(
                    f"  batch short {len(got)}/{len(bt)} (attempt {attempt + 1}/{DOWNLOAD_RETRIES})",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"  batch error attempt {attempt + 1}/{DOWNLOAD_RETRIES}: {e}",
                    flush=True,
                )
                got = {}
            sleep(DOWNLOAD_BACKOFF_SEC * (attempt + 1))
        out.update(got)
        missing = [s for s in bt if s not in out]
        if missing:
            print(f"  per-symbol retry for {len(missing)} missing", flush=True)
        if pause_sec:
            sleep(pause_sec)

    stats.yahoo = sum(1 for s in syms if s in out and not _needs_retry(out[s], expected_session, now))

    # --- 2) stale / missing: period=5d small batches, then Ticker.history ---
    # Hard wall-clock budget for this whole Yahoo retry phase.
    retry_start = clock()

    def retry_left() -> float:
        return _budget_left(clock, retry_start, retry_budget_sec)

    pending = _prioritize_pending(
        [s for s in syms if s not in out or _needs_retry(out.get(s), expected_session, now)],
        out,
    )
    if pending:
        print(
            f"stale/missing after main pass: {len(pending)}; Yahoo period=5d retry "
            f"(budget {retry_budget_sec:.0f}s)",
            flush=True,
        )
        retry_batch = 50
        for i in range(0, len(pending), retry_batch):
            if retry_left() <= 0:
                left = len(pending) - i
                print(
                    f"Yahoo 5d retry budget exhausted after {clock() - retry_start:.0f}s; "
                    f"skipping {left} symbols",
                    flush=True,
                )
                break
            bt = pending[i : i + retry_batch]
            timeout = min(batch_timeout_sec, max(0.1, retry_left()))
            try:
                raw = _run_with_timeout(
                    fetch, timeout, bt, period="5d", lookback_days=lookback_days, threads=True
                )
                got = _extract_batch_frames(raw, bt) if raw is not None else {}
            except Exception as e:
                print(f"  5d batch error: {e}", flush=True)
                got = {}
            for s, short in got.items():
                merged = _merge_yahoo(out.get(s), short)
                if not _needs_retry(merged, expected_session, now):
                    out[s] = merged
                    stats.retry += 1
                elif s in out:
                    out[s] = merged  # keep merged even if still stale (may help stooq splice)
            if pause_sec:
                sleep(min(pause_sec, max(0.0, retry_left())))

    still = _prioritize_pending(
        [s for s in syms if s not in out or _needs_retry(out.get(s), expected_session, now)],
        out,
    )
    if still and retry_left() > 0:
        # Prefer names that already have Yahoo history; cap how many we poke.
        cap = min(len(still), MAX_HISTORY_SYMBOLS)
        todo = still[:cap]
        print(
            f"per-symbol Ticker.history retry: {len(todo)}/{len(still)} "
            f"(budget left {retry_left():.0f}s, concurrency {retry_concurrency})",
            flush=True,
        )
        if hist_fn is None:
            def hist_fn(sym: str):
                import yfinance as yf

                return yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)

        def _hist_one(s: str):
            try:
                short = _run_with_timeout(hist_fn, request_timeout_sec, s)
                if short is None or getattr(short, "empty", True):
                    return s, None, False
                merged = _merge_yahoo(out.get(s), short)
                recovered = not _needs_retry(merged, expected_session, now)
                return s, merged, recovered
            except Exception as e:
                print(f"  {s}: history error {e}", flush=True)
                return s, None, False

        workers = max(1, int(retry_concurrency) or 1)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i in range(0, len(todo), workers):
                if retry_left() <= 0:
                    print(
                        f"Yahoo history retry budget exhausted after {clock() - retry_start:.0f}s; "
                        f"submitted {done}/{len(todo)}",
                        flush=True,
                    )
                    break
                chunk = todo[i : i + workers]
                futs = [pool.submit(_hist_one, s) for s in chunk]
                done += len(chunk)
                for fut in futs:
                    left = retry_left()
                    try:
                        s, merged, recovered = fut.result(timeout=max(0.1, left + request_timeout_sec))
                    except Exception:
                        continue
                    if merged is None:
                        continue
                    out[s] = merged
                    if recovered:
                        stats.retry += 1
    elif still:
        print(
            f"Yahoo retry budget exhausted; skipping Ticker.history for {len(still)} symbols",
            flush=True,
        )

    # --- 3) Stooq gap-fill only onto existing Yahoo history ---
    still = _prioritize_pending(
        [s for s in syms if s not in out or _needs_retry(out.get(s), expected_session, now)],
        out,
    )
    # Stooq can only splice onto an existing Yahoo prefix.
    still = [s for s in still if s in out and out[s] is not None and not getattr(out[s], "empty", True)]
    stooq_start = clock()
    if still:
        cap = min(len(still), MAX_STOOQ_SYMBOLS)
        todo = still[:cap]
        print(
            f"stooq gap-fill (Yahoo prefix required): {len(todo)}/{len(still)} "
            f"(budget {stooq_budget_sec:.0f}s)",
            flush=True,
        )

        def _stooq_one(s: str):
            try:
                sdf = _run_with_timeout(stooq_fn, request_timeout_sec, s)
            except Exception as e:
                print(f"  stooq {s}: {e}", flush=True)
                return s, None, 0
            if sdf is None:
                return s, None, 0
            merged, n_add = _append_stooq_gaps(out[s], sdf, expected_session)
            return s, merged if n_add else None, n_add

        workers = max(1, int(retry_concurrency) or 1)
        submitted = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i in range(0, len(todo), workers):
                if _budget_left(clock, stooq_start, stooq_budget_sec) <= 0:
                    print(
                        f"Stooq budget exhausted after {clock() - stooq_start:.0f}s; "
                        f"submitted {submitted}/{len(todo)}",
                        flush=True,
                    )
                    break
                chunk = todo[i : i + workers]
                futs = [pool.submit(_stooq_one, s) for s in chunk]
                submitted += len(chunk)
                for fut in futs:
                    left = _budget_left(clock, stooq_start, stooq_budget_sec)
                    try:
                        s, merged, n_add = fut.result(timeout=max(0.1, left + request_timeout_sec))
                    except Exception:
                        continue
                    if not n_add or merged is None:
                        continue
                    print(f"  stooq {s}: appended {n_add} bar(s) (adjustment may differ from Yahoo)", flush=True)
                    out[s] = merged
                    if not _needs_retry(merged, expected_session, now):
                        stats.stooq += 1

    # --- tally ---
    hist: dict[str, int] = {}
    for s in syms:
        frame = out.get(s)
        lb = last_bar_date(frame, now=now) if frame is not None else None
        if lb is None:
            stats.missing += 1
            continue
        hist[lb.isoformat()] = hist.get(lb.isoformat(), 0) + 1
        if lb == expected_session:
            stats.fresh += 1
        else:
            stats.stale_after += 1
    stats.last_bar_hist = dict(sorted(hist.items()))
    return out, stats
