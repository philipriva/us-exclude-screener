#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NYSE (XNYS) session calendar helpers — holidays, DST, early closes.

Adapted for us-exclude-screener. No ExtraScan coverage/.90 policy is applied here.

Fail-closed: if exchange_calendars cannot load XNYS, callers must not guess.
Do not replace with an incomplete hardcoded holiday list.
"""
from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")

# Approved Daily ExtraScan threshold — do not lower.


class CalendarUnavailableError(RuntimeError):
    """Raised when the XNYS calendar cannot be loaded (fail-closed)."""


@lru_cache(maxsize=1)
def xnys_calendar():
    try:
        import exchange_calendars as xcals
    except Exception as exc:  # pragma: no cover - import env
        raise CalendarUnavailableError(
            f"FAIL_CLOSED: exchange_calendars import failed: {exc}"
        ) from exc
    try:
        return xcals.get_calendar("XNYS")
    except Exception as exc:
        raise CalendarUnavailableError(
            f"FAIL_CLOSED: cannot load XNYS calendar: {exc}"
        ) from exc


def ny_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(NY)
    if now.tzinfo is None:
        return now.replace(tzinfo=NY)
    return now.astimezone(NY)


def session_bar_date(idx) -> date:
    """Calendar date of a daily bar's session label.

    Yahoo/yfinance daily bars are session-labeled. A tz-aware UTC midnight
    timestamp ``2026-11-05 00:00:00+00:00`` means the 2026-11-05 session, not
    the ET wall-clock of that instant (which would wrongly become 2026-11-04
    if converted with tz_convert). Strip tz without converting so the label
    date is preserved.
    """
    ts = pd.Timestamp(idx)
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return ts.date()


def _as_session_ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d)


def is_trading_session(d: date) -> bool:
    cal = xnys_calendar()
    return bool(cal.is_session(_as_session_ts(d)))


def session_close_et(d: date) -> datetime:
    """Regular or early close in America/New_York for a session date."""
    cal = xnys_calendar()
    ts = _as_session_ts(d)
    if not cal.is_session(ts):
        raise ValueError(f"not an XNYS session: {d.isoformat()}")
    close_utc = cal.session_close(ts)
    return close_utc.tz_convert(NY).to_pydatetime()


def is_early_close(d: date) -> bool:
    cal = xnys_calendar()
    ts = _as_session_ts(d)
    if not cal.is_session(ts):
        return False
    close_et = session_close_et(d)
    # Regular cash close is 16:00 ET; early close is typically 13:00 ET.
    return (close_et.hour, close_et.minute) < (16, 0)


def last_complete_us_session_date(now: datetime | None = None) -> date:
    """Prior complete US cash session (XNYS holidays + early closes).

    After that session's official close (incl. early close), the session date
    is complete. Before close — or on a holiday/weekend — return the previous
    XNYS session. Fail-closed via xnys_calendar().
    """
    cal = xnys_calendar()
    now = ny_now(now)
    d = now.date()
    ts = _as_session_ts(d)
    if cal.is_session(ts):
        close_et = session_close_et(d)
        if now >= close_et:
            return d
        prev = cal.previous_session(ts)
        return prev.date()
    # Non-session (weekend / full holiday): previous session ending at/before d.
    prev = cal.date_to_session(ts, direction="previous")
    return prev.date()


def drop_forming_bar(d: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop today's still-forming daily bar before the official XNYS close."""
    if d is None or d.empty:
        return d
    now = ny_now(now)
    last = session_bar_date(d.index[-1])
    if last != now.date():
        return d
    cal = xnys_calendar()
    ts = _as_session_ts(last)
    if not cal.is_session(ts):
        # Label claims "today" but today is not a session — treat as incomplete.
        return d.iloc[:-1]
    if now < session_close_et(last):
        return d.iloc[:-1]
    return d


def asof_session_str(session: date | None = None) -> str:
    session = session or last_complete_us_session_date()
    return f"{session.isoformat()} EOD ET"
