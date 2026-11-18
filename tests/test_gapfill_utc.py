"""Massive UTC-index gap-fill + trailing-NaN last bar (port of ExtraScan PR #25)."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

import marketdata as m

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
EXP = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 21, 16, tzinfo=NY)


def _stale(tz):
    idx = pd.to_datetime(["2026-11-13", "2026-11-16"])
    if tz is not None:
        idx = idx.tz_localize(tz)
    return pd.DataFrame(
        {"Open": [1, 1], "High": [1, 1], "Low": [1, 1], "Close": [1, 1], "Volume": [1, 1]},
        index=idx,
    )


def _fetch(d):
    # Grouped daily returns the whole session, as in production today.
    return {"AAA": {"Open": 2.0, "High": 2.0, "Low": 2.0, "Close": 2.0, "Volume": 2.0}}


def test_gap_fill_succeeds_when_yahoo_index_is_utc():
    frames = {"AAA": _stale(UTC)}
    n = m.massive_gap_fill(frames, ["AAA"], EXP, now=NOW, fetch=_fetch)
    assert n == 1
    assert m.last_bar_date(frames["AAA"], now=NOW) == EXP


def test_gap_fill_still_succeeds_when_yahoo_index_is_et_or_naive():
    for tz in (None, NY):
        frames = {"AAA": _stale(tz)}
        n = m.massive_gap_fill(frames, ["AAA"], EXP, now=NOW, fetch=_fetch)
        assert n == 1
        assert m.last_bar_date(frames["AAA"], now=NOW) == EXP


def test_nan_trailing_row_is_not_counted_fresh():
    idx = pd.to_datetime(["2026-11-16", "2026-11-17"]).tz_localize(NY)
    fr = pd.DataFrame(
        {"Open": [1, 1], "High": [1, 1], "Low": [1, 1], "Close": [1, float("nan")], "Volume": [1, 1]},
        index=idx,
    )
    assert m.last_bar_date(fr, now=NOW) == date(2026, 11, 16)
