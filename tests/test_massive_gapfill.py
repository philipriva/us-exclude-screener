"""Massive grouped-daily gap-fill: append only the missing session bar(s) onto
existing Yahoo history; never invent history; fail-soft without key."""
from datetime import date, datetime

import pandas as pd

import marketdata as m

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=m.NY)
EXP = date(2026, 11, 13)


def _frame(last: str, n: int = 30, tz=None):
    idx = pd.bdate_range(end=last, periods=n)
    if tz:
        idx = idx.tz_localize(tz)
    return pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 100.0}, index=idx
    )


def _bar(c):
    return {"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 10.0}


def test_fills_target_bar_and_maps_class_shares():
    frames = {"AAA": _frame("2026-11-12"), "BRK-B": _frame("2026-11-12", tz=m.NY), "OK": _frame("2026-11-13")}
    calls = []

    def fetch(d):
        calls.append(d)
        return {"AAA": _bar(9.0), m._massive_symbol("BRK.B"): _bar(592.94), "OK": _bar(1.0)}

    n = m.massive_gap_fill(frames, list(frames), EXP, now=NOW, fetch=fetch)
    assert n == 2
    assert calls == [EXP]  # one grouped request for the session
    assert m.last_bar_date(frames["AAA"], now=NOW) == EXP
    assert frames["AAA"]["Close"].iloc[-1] == 9.0
    assert frames["AAA"]["Close"].iloc[-2] == 1.5  # Yahoo prefix untouched
    assert frames["BRK-B"]["Close"].iloc[-1] == 592.94
    assert len(frames["OK"]) == 30  # already fresh: untouched


def test_no_history_not_created_and_missing_in_massive_skipped():
    frames = {"AAA": _frame("2026-11-12"), "NOPE": None}

    def fetch(d):
        return {"NOPE": _bar(1.0)}

    n = m.massive_gap_fill(frames, ["AAA", "NOPE", "GONE"], EXP, now=NOW, fetch=fetch)
    assert n == 0
    assert frames["NOPE"] is None and "GONE" not in frames
    assert m.last_bar_date(frames["AAA"], now=NOW) == date(2026, 11, 12)


def test_two_missing_sessions_need_both_no_gap():
    frames = {"AAA": _frame("2026-11-11"), "BBB": _frame("2026-11-11")}
    data = {date(2026, 11, 12): {"AAA": _bar(2.0), "BBB": _bar(2.0)}, EXP: {"AAA": _bar(3.0)}}
    n = m.massive_gap_fill(frames, ["AAA", "BBB"], EXP, now=NOW, fetch=lambda d: data[d])
    assert n == 1
    assert list(frames["AAA"]["Close"].iloc[-2:]) == [2.0, 3.0]
    assert m.last_bar_date(frames["BBB"], now=NOW) == date(2026, 11, 11)  # no gap-y partial fill


def test_too_old_not_filled():
    frames = {"OLD": _frame("2026-10-01")}
    called = []
    n = m.massive_gap_fill(frames, ["OLD"], EXP, now=NOW, fetch=lambda d: called.append(d) or {})
    assert n == 0 and called == []


def test_failed_fetch_and_missing_key_are_noop(monkeypatch):
    frames = {"AAA": _frame("2026-11-12")}
    assert m.massive_gap_fill(frames, ["AAA"], EXP, now=NOW, fetch=lambda d: None) == 0
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert m.massive_gap_fill(frames, ["AAA"], EXP, now=NOW) == 0
    assert m.last_bar_date(frames["AAA"], now=NOW) == date(2026, 11, 12)


def test_fetch_grouped_parses_and_caches(monkeypatch):
    import requests

    m._MASSIVE_CACHE.clear()
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    hits = []

    class R:
        status_code = 200

        def json(self):
            return {"results": [{"T": "BRK.B", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 7}, {"T": "BAD", "o": None}]}

    def get(url, **kw):
        hits.append(url)
        assert "apiKey" not in url and "k" not in str(kw.get("params"))
        return R()

    monkeypatch.setattr(requests, "get", get)
    a = m._fetch_massive_grouped(EXP)
    b = m._fetch_massive_grouped(EXP)
    assert a == b and list(a) == ["BRK-B"] and a["BRK-B"]["Close"] == 1.5
    assert len(hits) == 1
    m._MASSIVE_CACHE.clear()


def test_download_daily_uses_massive_before_stooq():
    stale = _frame("2026-11-12")
    stooq_calls = []

    def yf_download(tickers, **kw):
        return pd.concat({t: stale for t in tickers}, axis=1)

    st_out, stats = m.download_daily(
        ["AAA", "BBB"],
        EXP,
        now=NOW,
        yf_download=yf_download,
        yf_history=lambda s: None,
        stooq_fetch=lambda s: stooq_calls.append(s),
        massive_fetch=lambda d: {"AAA": _bar(5.0)},
        sleep=lambda s: None,
        retry_budget_sec=1.0,
        request_timeout_sec=1.0,
        batch_timeout_sec=5.0,
    )
    assert stats.massive == 1 and stats.fresh == 1
    assert m.last_bar_date(st_out["AAA"], now=NOW) == EXP
    assert stooq_calls == ["BBB"]  # Stooq only for what Massive could not fill


def test_status_line_massive_optional():
    kw = dict(session=EXP, writer="x", fresh=9, denom=10, yahoo=8, retry=0, stooq=0, result="WROTE")
    assert "massive=" not in m.format_status(**kw)
    assert "stooq=0 massive=5 result=WROTE" in m.format_status(massive=5, **kw)
