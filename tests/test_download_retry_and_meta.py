#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests: empty/rate-limit retry, NA filter, no silent Sheet write, routing plan."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_exclude as rexc
import us_session


def _ohlc_frame(days=30, last: date | None = None) -> pd.DataFrame:
    last = last or date(2026, 11, 5)
    idx = pd.bdate_range(end=pd.Timestamp(last), periods=days)
    return pd.DataFrame(
        {
            "Open": 10.0,
            "High": 11.0,
            "Low": 9.0,
            "Close": 10.5,
            "Volume": 1_000_000,
        },
        index=idx,
    )


def test_parse_syms_rejects_nan_token():
    assert rexc.parse_syms("AAPL NAN none NULL TSLA") == ["AAPL", "TSLA"]


def test_download_empty_no_throw_retries_then_fills(monkeypatch):
    """yfinance returns empty (no exception) → must retry, not silent-skip forever."""
    calls = {"n": 0}

    def fake_download(tickers, **kwargs):
        calls["n"] += 1
        # First two rounds: empty frame (rate-limit style silent miss)
        if calls["n"] < 3:
            return pd.DataFrame()
        # Third round: return multi-ticker columns
        frames = {t: _ohlc_frame() for t in tickers}
        return pd.concat(frames, axis=1)

    monkeypatch.setattr(rexc.yf, "download", fake_download)
    monkeypatch.setattr(rexc.time, "sleep", lambda *_a, **_k: None)
    data, errors = rexc.download(["AAA", "BBB"], max_rounds=4, batch_size=2)
    assert set(data) == {"AAA", "BBB"}
    assert errors == {}
    assert calls["n"] >= 3


def test_download_exception_path_retries(monkeypatch):
    calls = {"n": 0}

    def fake_download(tickers, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("YFRateLimitError boom")
        frames = {t: _ohlc_frame() for t in tickers}
        return pd.concat(frames, axis=1)

    monkeypatch.setattr(rexc.yf, "download", fake_download)
    monkeypatch.setattr(rexc.time, "sleep", lambda *_a, **_k: None)
    data, errors = rexc.download(["CCC"], max_rounds=3, batch_size=1)
    assert "CCC" in data and errors == {}


def test_download_exhaustion_keeps_error(monkeypatch):
    monkeypatch.setattr(rexc.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(rexc.time, "sleep", lambda *_a, **_k: None)
    data, errors = rexc.download(["ZZZ"], max_rounds=2, batch_size=1)
    assert data == {}
    assert "ZZZ" in errors
    assert "empty_or_rate_limited" in errors["ZZZ"]


def test_classify_separates_missing_short_stale_and_ok():
    target = date(2026, 11, 5)
    data = {
        "OK": _ohlc_frame(30, last=target),
        "STALE": _ohlc_frame(30, last=date(2026, 11, 4)),
        "SHORT": _ohlc_frame(3, last=target),
    }
    errors = {"MISS": "empty_or_rate_limited_no_ohlc"}
    out = rexc.classify_ticker_bars(data, errors, ["OK", "STALE", "SHORT", "MISS"], target)
    c = out["counts"]
    assert c["ok_target_session"] == 1
    assert c["stale"] == 1
    assert c["short_history"] == 1
    assert c["missing"] == 1
    assert out["per_ticker"]["OK"]["signal_eligible_min_bars"] is True
    assert out["per_ticker"]["SHORT"]["signal_eligible_min_bars"] is False
    assert out["per_ticker"]["MISS"]["last_bar_date"] is None


def test_sheets_blocked_when_incomplete(tmp_path, monkeypatch):
    """Incomplete download must not call Sheets writer."""
    wrote = {"n": 0}

    def boom(*a, **k):
        wrote["n"] += 1
        raise AssertionError("Sheets must not be called")

    monkeypatch.setattr(rexc, "maybe_write_sheets", boom)
    monkeypatch.setattr(rexc, "universe", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(
        rexc,
        "download",
        lambda syms, **k: ({"AAA": _ohlc_frame()}, {"BBB": "empty_or_rate_limited_no_ohlc"}),
    )
    monkeypatch.setattr(rexc, "eval_gates", lambda *a, **k: {g: False for g in rexc.GATE_ORDER if g != "04_mcap"})
    monkeypatch.setattr(
        rexc,
        "last_complete_us_session_date",
        lambda: date(2026, 11, 5),
    )
    # avoid mcap network
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_exclude.py", "--out-dir", str(tmp_path), "--gates", "02_ema200"],
    )
    rc = rexc.main()
    assert rc == 1
    assert wrote["n"] == 0
    meta = json.loads((tmp_path / "run_outcome.json").read_text())
    assert meta["download_complete"] is False
    assert meta["ready_for_sheets"] is False
    assert meta["policy"]["coverage_threshold"] is None
    assert meta["sheet_target_plan"]["gates"]["mode"] == "separate_spreadsheets_per_gate"


def test_us_session_not_execution_day_filler():
    # Friday 2026-11-06 15:00 ET → last complete is 2026-11-05
    now = datetime(2026, 11, 6, 15, 0, tzinfo=us_session.NY)
    assert us_session.last_complete_us_session_date(now) == date(2026, 11, 5)


def test_routing_plan_does_not_claim_main_gate_tabs():
    src = Path(rexc.__file__).read_text(encoding="utf-8")
    assert "separate_spreadsheets_per_gate" in src
    assert "Do NOT silently rewrite exclude_* tabs on the combine workbook" in src
    assert "unless Philip explicitly authorizes that contract change." in src
    assert "optional_also_write_gate_tabs_on_combine_workbook" in src
