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
SYMBOL_RETRY_RETRIES = 3
STOOQ_TIMEOUT_SEC = 20

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


def _mcal_schedule(d: date):
    try:
        import pandas_market_calendars as mcal
    except Exception:
        return None
    try:
        nyse = mcal.get_calendar("XNYS")
        sched = nyse.schedule(start_date=d.isoformat(), end_date=d.isoformat())
        return sched
    except Exception:
        return None


def is_nyse_trading_day(d: date) -> bool:
    sched = _mcal_schedule(d)
    if sched is not None:
        return len(sched) > 0
    return _stdlib_is_trading_day(d)


def session_close_datetime(session: date) -> datetime:
    """Official NYSE cash close in America/New_York (16:00, or 13:00 early close)."""
    sched = _mcal_schedule(session)
    if sched is not None and len(sched) > 0:
        close = sched.iloc[0]["market_close"]
        return close.tz_convert(NY).to_pydatetime()
    hour, minute = (13, 0) if _stdlib_is_early_close(session) else (16, 0)
    return datetime(session.year, session.month, session.day, hour, minute, tzinfo=NY)


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
) -> tuple[dict, DownloadStats]:
    """Fetch daily bars; Yahoo primary, Stooq only fills last-session gaps.

    yf_download / yf_history / stooq_fetch are injectable for tests.
    """
    syms = [str(s).strip() for s in symbols if str(s).strip()]
    stats = DownloadStats(requested=len(syms))
    out: dict = {}
    if not syms:
        return out, stats

    fetch = yf_download or (lambda tickers, **kw: _yf_download_once(tickers, **kw))
    hist_fn = yf_history
    stooq_fn = stooq_fetch if stooq_fetch is not None else _fetch_stooq

    # --- 1) main Yahoo batch ---
    n = len(syms)
    for i in range(0, n, batch_size):
        bt = syms[i : i + batch_size]
        print(f"download {min(i + batch_size, n)}/{n}", flush=True)
        got = {}
        for attempt in range(DOWNLOAD_RETRIES):
            try:
                raw = fetch(bt, period=period, lookback_days=lookback_days, threads=True)
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
    pending = [s for s in syms if s not in out or _needs_retry(out.get(s), expected_session, now)]
    if pending:
        print(f"stale/missing after main pass: {len(pending)}; Yahoo period=5d retry", flush=True)
        retry_batch = 50
        for i in range(0, len(pending), retry_batch):
            bt = pending[i : i + retry_batch]
            try:
                raw = fetch(bt, period="5d", lookback_days=lookback_days, threads=True)
                got = _extract_batch_frames(raw, bt)
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
            sleep(DOWNLOAD_BACKOFF_SEC)

    still = [s for s in syms if s not in out or _needs_retry(out.get(s), expected_session, now)]
    if still:
        print(f"per-symbol Ticker.history retry: {len(still)}", flush=True)
        if hist_fn is None:
            def hist_fn(sym: str):
                import yfinance as yf

                return yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)

        for s in still:
            ok = False
            for attempt in range(SYMBOL_RETRY_RETRIES):
                try:
                    short = hist_fn(s)
                    if short is None or getattr(short, "empty", True):
                        sleep(DOWNLOAD_BACKOFF_SEC * (attempt + 1))
                        continue
                    merged = _merge_yahoo(out.get(s), short)
                    out[s] = merged
                    if not _needs_retry(merged, expected_session, now):
                        stats.retry += 1
                        ok = True
                        break
                except Exception as e:
                    print(f"  {s}: history error {e} (retry {attempt + 1})", flush=True)
                sleep(DOWNLOAD_BACKOFF_SEC * (attempt + 1))
            # ok unused; still-stale goes to Stooq

    # --- 3) Stooq gap-fill only onto existing Yahoo history ---
    still = [s for s in syms if s not in out or _needs_retry(out.get(s), expected_session, now)]
    if still:
        print(f"stooq gap-fill (Yahoo prefix required): {len(still)}", flush=True)
        for s in still:
            if s not in out or out[s] is None or getattr(out[s], "empty", True):
                # No Yahoo history — do not replace with Stooq.
                continue
            try:
                sdf = stooq_fn(s)
            except Exception as e:
                print(f"  stooq {s}: {e}", flush=True)
                continue
            if sdf is None:
                continue
            merged, n_add = _append_stooq_gaps(out[s], sdf, expected_session)
            if n_add:
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
