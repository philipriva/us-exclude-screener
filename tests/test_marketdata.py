import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import marketdata as m

NY = ZoneInfo("America/New_York")


def _frame(closes, start="2026-11-02", cols=("Open", "High", "Low", "Close", "Volume")):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    data = {}
    for c in cols:
        if c == "Volume":
            data[c] = [1_000_000] * len(closes)
        elif c == "Close":
            data[c] = closes
        else:
            data[c] = [x + 0.1 if c == "High" else x - 0.1 if c == "Low" else x for x in closes]
    return pd.DataFrame(data, index=idx)


# ---------- calendar ----------

def test_regular_session_close_16et_dst_and_standard():
    assert m.session_close_utc(date(2026, 11, 9)).isoformat() == "2026-11-09T21:00:00+00:00"  # EST
    assert m.session_close_utc(date(2026, 10, 30)).isoformat() == "2026-10-30T20:00:00+00:00"  # EDT


def test_last_complete_regular_weekdays():
    assert m.last_complete_us_session_date(datetime(2026, 11, 6, 15, 59, tzinfo=NY)) == date(2026, 11, 5)
    assert m.last_complete_us_session_date(datetime(2026, 11, 6, 16, 0, tzinfo=NY)) == date(2026, 11, 6)
    assert m.last_complete_us_session_date(datetime(2026, 11, 7, 12, 0, tzinfo=NY)) == date(2026, 11, 6)
    assert m.last_complete_us_session_date(datetime(2026, 11, 8, 9, 0, tzinfo=NY)) == date(2026, 11, 6)
    assert m.last_complete_us_session_date(datetime(2026, 11, 9, 10, 0, tzinfo=NY)) == date(2026, 11, 6)


def test_thanksgiving_holiday_and_black_friday_early_close():
    # 2026-11-26 Thanksgiving (Thursday) is a full holiday
    assert m.is_nyse_trading_day(date(2026, 11, 26)) is False
    # Wednesday close 16:00 → session is 11-25; after 16:00 still 11-25 (Thu is holiday)
    assert m.last_complete_us_session_date(datetime(2026, 11, 26, 17, 0, tzinfo=NY)) == date(2026, 11, 25)
    # Black Friday 2026-11-27 early close 13:00 ET
    assert m.is_nyse_trading_day(date(2026, 11, 27)) is True
    close = m.session_close_datetime(date(2026, 11, 27))
    assert (close.hour, close.minute) == (13, 0)
    assert m.last_complete_us_session_date(datetime(2026, 11, 27, 12, 59, tzinfo=NY)) == date(2026, 11, 25)
    assert m.last_complete_us_session_date(datetime(2026, 11, 27, 13, 0, tzinfo=NY)) == date(2026, 11, 27)


def test_christmas_eve_early_close_2026():
    # 2026-12-24 Thursday is a trading day with 13:00 close; 12-25 is holiday
    assert m.is_nyse_trading_day(date(2026, 12, 24)) is True
    assert m.session_close_datetime(date(2026, 12, 24)).hour == 13
    assert m.is_nyse_trading_day(date(2026, 12, 25)) is False
    assert m.last_complete_us_session_date(datetime(2026, 12, 25, 17, 0, tzinfo=NY)) == date(2026, 12, 24)


def test_one_off_mourning_day_2025_01_09():
    assert m.is_nyse_trading_day(date(2025, 1, 9)) is False
    # previous session 2025-01-08
    assert m.last_complete_us_session_date(datetime(2025, 1, 9, 17, 0, tzinfo=NY)) == date(2025, 1, 8)


def test_stdlib_fallback_matches_mcal_2020_2030():
    mcal = pytest.importorskip("pandas_market_calendars")
    nyse = mcal.get_calendar("XNYS")
    start, end = date(2020, 1, 1), date(2030, 12, 31)
    sched = nyse.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    trading = set(sched.index.date)
    early = set(nyse.early_closes(sched).index.date)
    d = start
    mismatches = []
    while d <= end:
        std_trade = m._stdlib_is_trading_day(d)
        std_early = m._stdlib_is_early_close(d)
        want_trade = d in trading
        want_early = d in early
        if std_trade != want_trade or (std_trade and std_early != want_early):
            mismatches.append((d, std_trade, want_trade, std_early, want_early))
        d += timedelta(days=1)
    assert mismatches == []


# ---------- drop_forming / bar_date ----------

def test_drop_forming_keeps_bar_after_close():
    d = _frame([10, 11], start="2026-11-10")  # last 11-11
    now = datetime(2026, 11, 11, 16, 0, tzinfo=NY)
    out = m.drop_forming(d, now=now)
    assert m.bar_date(out.index[-1]) == date(2026, 11, 11)


def test_drop_forming_drops_before_close():
    d = _frame([10, 11], start="2026-11-10")
    now = datetime(2026, 11, 11, 15, 0, tzinfo=NY)
    out = m.drop_forming(d, now=now)
    assert m.bar_date(out.index[-1]) == date(2026, 11, 10)


def test_bar_date_tz_aware_et():
    idx = pd.DatetimeIndex(["2026-11-11 00:00:00"]).tz_localize(NY)
    assert m.bar_date(idx[0]) == date(2026, 11, 11)


# ---------- freshness denom ----------

def test_freshness_screened_vs_universe_denom():
    bars = [date(2026, 11, 11)] * 9 + [date(2026, 11, 10)]
    ok_s, info_s = m.freshness_ok(bars, date(2026, 11, 11))  # 9/10 = 0.9
    assert ok_s is True and info_s["frac"] == 0.9
    ok_u, info_u = m.freshness_ok(bars, date(2026, 11, 11), universe_n=20)  # 9/20
    assert ok_u is False and info_u["fresh"] == 9 and info_u["universe"] == 20


# ---------- download_daily retry / stooq / blocked ----------

def test_download_daily_stale_then_yahoo_retry_passes():
    expected = date(2026, 11, 11)
    stale = _frame([10, 11], start="2026-11-09")  # ends 11-10
    fresh = _frame([10, 11, 12], start="2026-11-09")  # ends 11-11
    calls = {"n": 0}

    def yf_download(tickers, **kw):
        calls["n"] += 1
        period = kw.get("period")
        if period == "5d":
            return _multi({t: fresh for t in tickers})
        return _multi({t: stale for t in tickers})

    frames, stats = m.download_daily(
        ["SPY", "QQQ"],
        expected,
        yf_download=yf_download,
        yf_history=lambda s: fresh,
        stooq_fetch=lambda s: None,
        sleep=lambda *_a, **_k: None,
        now=datetime(2026, 11, 11, 18, 0, tzinfo=NY),
    )
    assert stats.fresh == 2 and stats.retry == 2 and stats.stooq == 0
    assert m.last_bar_date(frames["SPY"], now=datetime(2026, 11, 11, 18, 0, tzinfo=NY)) == expected
    ok, _ = stats.freshness(expected, full_universe=True)
    assert ok is True


def test_download_daily_stooq_appends_only_missing_latest_bar():
    expected = date(2026, 11, 11)
    yahoo = _frame([10.0, 11.0], start="2026-11-09")  # ends 11-10, close 11
    # Stooq has a longer history with a different 11-10 close — must NOT replace Yahoo
    stooq = _frame([1.0, 2.0, 99.0], start="2026-11-09")  # 11-09, 11-10, 11-11

    def yf_download(tickers, **kw):
        return _multi({t: yahoo for t in tickers})

    frames, stats = m.download_daily(
        ["SPY"],
        expected,
        yf_download=yf_download,
        yf_history=lambda s: yahoo,
        stooq_fetch=lambda s: stooq,
        sleep=lambda *_a, **_k: None,
        now=datetime(2026, 11, 11, 18, 0, tzinfo=NY),
    )
    d = frames["SPY"]
    # Yahoo 11-10 close is 11.0, not Stooq's 2.0
    row_1110 = d.loc[d.index.map(m.bar_date) == date(2026, 11, 10)].iloc[0]
    assert float(row_1110["Close"]) == 11.0
    assert m.last_bar_date(d, now=datetime(2026, 11, 11, 18, 0, tzinfo=NY)) == expected
    assert stats.stooq == 1
    # appended 11-11 close comes from Stooq (99)
    row_1111 = d.loc[d.index.map(m.bar_date) == date(2026, 11, 11)].iloc[0]
    assert float(row_1111["Close"]) == 99.0


def test_download_daily_no_yahoo_history_does_not_use_stooq_replacement():
    expected = date(2026, 11, 11)
    stooq = _frame([10, 11, 12], start="2026-11-09")

    def yf_download(tickers, **kw):
        return pd.DataFrame()

    frames, stats = m.download_daily(
        ["ZZZZ"],
        expected,
        yf_download=yf_download,
        yf_history=lambda s: pd.DataFrame(),
        stooq_fetch=lambda s: stooq,
        sleep=lambda *_a, **_k: None,
        now=datetime(2026, 11, 11, 18, 0, tzinfo=NY),
    )
    assert "ZZZZ" not in frames
    assert stats.stooq == 0 and stats.fresh == 0 and stats.missing == 1
    ok, _ = stats.freshness(expected, full_universe=True)
    assert ok is False


def test_download_daily_still_stale_blocked():
    expected = date(2026, 11, 11)
    stale = _frame([10, 11], start="2026-11-09")  # ends 11-10

    def yf_download(tickers, **kw):
        return _multi({t: stale for t in tickers})

    frames, stats = m.download_daily(
        ["SPY", "QQQ", "IWM"],
        expected,
        yf_download=yf_download,
        yf_history=lambda s: stale,
        stooq_fetch=lambda s: stale,
        sleep=lambda *_a, **_k: None,
        now=datetime(2026, 11, 11, 18, 0, tzinfo=NY),
    )
    assert stats.fresh == 0 and stats.stale_after == 3
    ok, info = stats.freshness(expected, full_universe=True)
    assert ok is False
    assert info["frac"] == 0.0


def test_format_status_line():
    line = m.format_status(
        session=date(2026, 11, 11),
        writer="CDEF",
        fresh=6252,
        denom=6604,
        yahoo=6000,
        retry=200,
        stooq=52,
        result="WROTE",
        extra="ETF=125/126=0.9921",
    )
    assert line.startswith("STATUS session=2026-11-11 writer=CDEF fresh=6252/6604=0.9467")
    assert "yahoo=6000 retry=200 stooq=52 result=WROTE" in line


def _multi(mapping: dict):
    """Build a yfinance-like multi-ticker frame (columns MultiIndex ticker, field)."""
    if len(mapping) == 1:
        return next(iter(mapping.values()))
    return pd.concat({k: v for k, v in mapping.items()}, axis=1)


def test_retry_budget_stops_and_evaluates_freshness():
    """5d/history retry must stop when the wall-clock budget is gone; gate still runs."""
    stale = _frame([10, 11], start="2026-11-09")  # ends 11-10
    fresh = _frame([10, 11, 12], start="2026-11-09")  # ends 11-11
    clock = {"t": 0.0}
    calls_5d = {"n": 0}

    def yf_download(tickers, **kw):
        if kw.get("period") == "5d":
            calls_5d["n"] += 1
            clock["t"] += 100.0
            return _multi({t: fresh for t in tickers})
        return _multi({t: stale for t in tickers})

    def yf_history(s):
        clock["t"] += 100.0
        return fresh

    frames, stats = m.download_daily(
        ["AAA", "BBB", "CCC"],
        date(2026, 11, 11),
        yf_download=yf_download,
        yf_history=yf_history,
        stooq_fetch=lambda s: None,
        sleep=lambda s: None,
        clock=lambda: clock["t"],
        retry_budget_sec=50.0,
        stooq_budget_sec=0.0,
        retry_concurrency=1,
        pause_sec=0,
    )
    # One 5d batch burns 100s of a 50s budget → no history phase.
    assert calls_5d["n"] == 1
    # 5d batch itself is allowed to finish (budget checked at the start of each batch).
    assert stats.fresh == 3
    assert "AAA" in frames


def test_retry_budget_skips_history_when_already_exhausted():
    stale = _frame([10, 11], start="2026-11-09")
    clock = {"t": 0.0}
    hist_calls = []

    def yf_download(tickers, **kw):
        if kw.get("period") == "5d":
            clock["t"] += 100.0
            return _multi({t: stale for t in tickers})
        return _multi({t: stale for t in tickers})

    frames, stats = m.download_daily(
        ["AAA", "BBB", "CCC"],
        date(2026, 11, 11),
        yf_download=yf_download,
        yf_history=lambda s: hist_calls.append(s) or stale,
        stooq_fetch=lambda s: None,
        sleep=lambda s: None,
        clock=lambda: clock["t"],
        retry_budget_sec=50.0,
        stooq_budget_sec=0.0,
        retry_concurrency=1,
        pause_sec=0,
    )
    assert hist_calls == []
    assert stats.fresh == 0 and stats.stale_after == 3
    assert set(frames) == {"AAA", "BBB", "CCC"}


def test_hanging_history_times_out_and_does_not_block():
    stale = _frame([10, 11], start="2026-11-09")

    def yf_download(tickers, **kw):
        return _multi({t: stale for t in tickers})

    def hang(_s):
        time.sleep(30)
        return stale

    import time as _t
    t0 = _t.monotonic()
    frames, stats = m.download_daily(
        ["AAA"],
        date(2026, 11, 11),
        yf_download=yf_download,
        yf_history=hang,
        stooq_fetch=lambda s: None,
        sleep=lambda s: None,
        retry_budget_sec=60.0,
        stooq_budget_sec=0.0,
        request_timeout_sec=0.3,
        retry_concurrency=1,
        pause_sec=0,
    )
    elapsed = _t.monotonic() - t0
    assert elapsed < 5.0, elapsed
    assert stats.fresh == 0 and stats.stale_after == 1
    assert "AAA" in frames


def test_stooq_budget_stops_after_cap():
    stale = _frame([10, 11], start="2026-11-09")  # ends 11-10
    stooq_fresh = _frame([10, 11, 12], start="2026-11-09")  # ends 11-11
    clock = {"t": 0.0}
    calls = []

    def yf_download(tickers, **kw):
        return _multi({t: stale for t in tickers})

    def stooq(s):
        calls.append(s)
        clock["t"] += 100.0
        return stooq_fresh

    frames, stats = m.download_daily(
        ["AAA", "BBB", "CCC"],
        date(2026, 11, 11),
        yf_download=yf_download,
        yf_history=lambda s: stale,
        stooq_fetch=stooq,
        sleep=lambda s: None,
        clock=lambda: clock["t"],
        retry_budget_sec=0.0,  # skip yahoo retry; go to stooq with existing prefix
        stooq_budget_sec=50.0,
        retry_concurrency=1,
        pause_sec=0,
    )
    # First Stooq call burns 100s of a 50s budget; later symbols skipped.
    assert calls == ["AAA"]
    assert stats.stooq == 1
    assert stats.fresh == 1 and stats.stale_after == 2
