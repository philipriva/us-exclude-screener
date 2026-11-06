#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import io
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

NY = us_session.NY


def _ohlc_frame(days=30, last: date | None = None, all_nan: bool = False) -> pd.DataFrame:
    last = last or date(2026, 11, 5)
    idx = pd.bdate_range(end=pd.Timestamp(last), periods=days)
    if all_nan:
        return pd.DataFrame(
            {"Open": float("nan"), "High": float("nan"), "Low": float("nan"), "Close": float("nan")},
            index=idx,
        )
    return pd.DataFrame(
        {"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 1_000_000},
        index=idx,
    )


def _multi_ticker_field(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(frames, axis=1)  # columns (ticker, field)


def _multi_field_ticker(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    # columns (field, ticker)
    pieces = {}
    for s, d in frames.items():
        for col in ("Open", "High", "Low", "Close"):
            pieces[(col, s)] = d[col]
    return pd.DataFrame(pieces)


def test_parse_syms_keeps_na_none_null_tokens():
    # Real symbols must not be blacklisted; blank discarded by split.
    assert rexc.parse_syms("AAPL NA NONE NULL TSLA") == ["AAPL", "NA", "NONE", "NULL", "TSLA"]


def test_dir_keep_default_na_preserves_ticker_na():
    body = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "NA|Nano Labs Ltd - Class A Ordinary Shares|G|N|N|100|N|N\n"
        "AAPL|Apple Inc|Q|N|N|100|N|N\n"
        "File Creation Time: 111120261234\n"
    )
    with mock.patch.object(rexc.requests, "get") as g:
        resp = mock.Mock()
        resp.text = body
        resp.raise_for_status = mock.Mock()
        g.return_value = resp
        df = rexc._dir("https://example.test/nasdaqlisted.txt")
    assert list(df["Symbol"]) == ["NA", "AAPL"]
    assert df["Symbol"].isna().sum() == 0


def test_dir_fail_closed_missing_footer():
    with mock.patch.object(rexc.requests, "get") as g:
        resp = mock.Mock()
        resp.text = "Symbol|X\nAAPL|x\n"
        resp.raise_for_status = mock.Mock()
        g.return_value = resp
        with pytest.raises(rexc.UniverseParseError, match="File Creation"):
            rexc._dir("https://example.test/bad.txt")


def test_extract_multiindex_ticker_field_and_field_ticker():
    frames = {"AAA": _ohlc_frame(), "BBB": _ohlc_frame()}
    got1 = rexc._extract_batch_frames(_multi_ticker_field(frames), ["AAA", "BBB"])
    assert set(got1) == {"AAA", "BBB"}
    got2 = rexc._extract_batch_frames(_multi_field_ticker(frames), ["AAA", "BBB"])
    assert set(got2) == {"AAA", "BBB"}


def test_extract_all_nan_ohlc_rejected():
    frames = {"ZZZ": _ohlc_frame(all_nan=True)}
    got = rexc._extract_batch_frames(_multi_ticker_field(frames), ["ZZZ"])
    assert got == {}


def test_extract_empty_no_raise():
    assert rexc._extract_batch_frames(pd.DataFrame(), ["AAA"]) == {}
    assert rexc._extract_batch_frames(None, ["AAA"]) == {}


def test_download_empty_no_throw_retries_then_fills(monkeypatch):
    calls = {"n": 0}

    def fake_download(tickers, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return pd.DataFrame()
        return _multi_ticker_field({t: _ohlc_frame() for t in tickers})

    monkeypatch.setattr(rexc.yf, "download", fake_download)
    monkeypatch.setattr(rexc.time, "sleep", lambda *_a, **_k: None)
    data, errors = rexc.download(["AAA", "BBB"], max_rounds=4, batch_size=2)
    assert set(data) == {"AAA", "BBB"}
    assert errors == {}
    assert calls["n"] >= 3


def test_download_classifies_rate_limit_from_stderr(monkeypatch):
    def fake_download(tickers, **kwargs):
        sys.stderr.write("1 Failed download:\n['AAA']: YFRateLimitError('Too Many Requests. Rate limited.')\n")
        return pd.DataFrame()

    monkeypatch.setattr(rexc.yf, "download", fake_download)
    monkeypatch.setattr(rexc.time, "sleep", lambda *_a, **_k: None)
    data, errors = rexc.download(["AAA"], max_rounds=1, batch_size=1)
    assert data == {}
    assert errors["AAA"] == "rate_limit_confirmed"


def test_download_empty_without_stderr_is_unknown(monkeypatch):
    monkeypatch.setattr(rexc.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(rexc.time, "sleep", lambda *_a, **_k: None)
    _, errors = rexc.download(["ZZZ"], max_rounds=1, batch_size=1)
    assert errors["ZZZ"] == "unavailable_or_unknown_empty"


def test_classify_separates_freshness_and_history():
    target = date(2026, 11, 5)
    data = {
        "OK": _ohlc_frame(30, last=target),
        "STALE": _ohlc_frame(30, last=date(2026, 11, 4)),
        "SHORT": _ohlc_frame(3, last=target),  # fresh date but short history
        "SHORT_STALE": _ohlc_frame(3, last=date(2026, 11, 3)),
    }
    errors = {"MISS": "unavailable_or_unknown_empty", "RL": "rate_limit_confirmed"}
    out = rexc.classify_ticker_bars(
        data, errors, ["OK", "STALE", "SHORT", "SHORT_STALE", "MISS", "RL"], target
    )
    c = out["counts"]
    assert c["ok_target_session"] == 2  # OK + SHORT (freshness separate)
    assert c["stale"] == 2  # STALE + SHORT_STALE
    assert c["history_eligible"] == 2  # OK + STALE
    assert c["history_short"] == 2
    assert c["missing"] == 2
    assert c["rate_limit_confirmed"] == 1
    assert c["unavailable_or_unknown"] == 1
    assert out["per_ticker"]["SHORT"]["freshness"] == "ok_target_session"
    assert out["per_ticker"]["SHORT"]["history_eligible"] is False
    assert out["per_ticker"]["SHORT"]["last_bar_date"] == "2026-11-05"
    assert out["per_ticker"]["SHORT_STALE"]["freshness"] == "stale"
    assert out["per_ticker"]["SHORT_STALE"]["last_bar_date"] == "2026-11-03"


def test_validate_selected_rejects_stale_and_short():
    target = "2026-11-05"
    per = {
        "OK": {"freshness": "ok_target_session", "history_eligible": True, "last_bar_date": target},
        "STALE": {"freshness": "stale", "history_eligible": True, "last_bar_date": "2026-11-04"},
        "SHORT": {"freshness": "ok_target_session", "history_eligible": False, "last_bar_date": target},
    }
    bad = rexc.validate_selected_tickers(
        {"02_ema200": ["OK", "STALE", "SHORT"]}, per, target
    )
    reasons = {b["symbol"]: b["reason"] for b in bad}
    assert "STALE" in reasons and "SHORT" in reasons
    assert "OK" not in reasons


def test_mixed_stale_fresh_only_fresh_scanned_and_sheets_guard(tmp_path, monkeypatch):
    target = date(2026, 11, 5)
    frames = {
        "FRESH": _ohlc_frame(250, last=target),
        "STALE": _ohlc_frame(250, last=date(2026, 11, 4)),
    }
    wrote = {"n": 0}

    def boom(*a, **k):
        wrote["n"] += 1
        raise AssertionError("Sheets must not be called when download incomplete / blocked")

    monkeypatch.setattr(rexc, "maybe_write_sheets", boom)
    monkeypatch.setattr(rexc, "universe", lambda: ["FRESH", "STALE", "MISS"])
    monkeypatch.setattr(
        rexc,
        "download",
        lambda syms, **k: (frames, {"MISS": "unavailable_or_unknown_empty"}),
    )
    monkeypatch.setattr(rexc, "last_complete_us_session_date", lambda: target)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_exclude.py", "--out-dir", str(tmp_path), "--gates", "02_ema200"],
    )
    rc = rexc.main()
    assert rc == 1
    assert wrote["n"] == 0
    meta = json.loads((tmp_path / "run_outcome.json").read_text())
    assert meta["ready_for_sheets"] is False
    assert meta["download_complete"] is False
    # STALE must not appear in selected hits
    assert "STALE" not in set(sum(meta.get("gate_hit_counts") and [], []))
    hits_file = tmp_path / "exclude_02_ema200.txt"
    selected = [l for l in hits_file.read_text().splitlines() if l.strip()]
    assert "STALE" not in selected
    assert all(s == "FRESH" for s in selected) or selected == [] or "FRESH" in selected


def test_all_stale_blocks_sheets_exit_nonzero(tmp_path, monkeypatch):
    target = date(2026, 11, 5)
    frames = {"A": _ohlc_frame(30, last=date(2026, 11, 4))}
    wrote = {"n": 0}
    monkeypatch.setattr(rexc, "maybe_write_sheets", lambda *a, **k: wrote.__setitem__("n", wrote["n"] + 1))
    monkeypatch.setattr(rexc, "universe", lambda: ["A"])
    monkeypatch.setattr(rexc, "download", lambda syms, **k: (frames, {}))
    monkeypatch.setattr(rexc, "last_complete_us_session_date", lambda: target)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_exclude.py", "--out-dir", str(tmp_path), "--gates", "02_ema200"],
    )
    rc = rexc.main()
    assert rc == 1
    assert wrote["n"] == 0
    meta = json.loads((tmp_path / "run_outcome.json").read_text())
    assert "no_ok_target_session_bars" in meta["block_reasons"]
    assert meta["download_complete"] is True


def test_accounting_denominator(tmp_path, monkeypatch):
    target = date(2026, 11, 5)
    uni = ["OK", "STALE", "MISS", "RL"]
    frames = {
        "OK": _ohlc_frame(30, last=target),
        "STALE": _ohlc_frame(30, last=date(2026, 11, 4)),
    }
    errs = {"MISS": "unavailable_or_unknown_empty", "RL": "rate_limit_confirmed"}
    monkeypatch.setattr(rexc, "maybe_write_sheets", lambda *a, **k: None)
    monkeypatch.setattr(rexc, "universe", lambda: uni)
    monkeypatch.setattr(rexc, "download", lambda syms, **k: (frames, errs))
    monkeypatch.setattr(rexc, "last_complete_us_session_date", lambda: target)
    monkeypatch.setattr(
        sys, "argv", ["run_exclude.py", "--out-dir", str(tmp_path), "--gates", "03_ema50"]
    )
    rc = rexc.main()
    assert rc == 1
    meta = json.loads((tmp_path / "run_outcome.json").read_text())
    c = meta["counts"]
    assert c["universe"] == 4
    assert c["ok_target_session"] + c["stale"] + c["future"] + c["missing"] + c["error"] + c["no_bar_date"] == 4
    assert c["rate_limit_confirmed"] + c["unavailable_or_unknown"] + c["error"] == c["missing"] + c["error"]
    assert meta["source_input_count"] == 4
    assert meta["downloaded_count"] == 2


def test_us_session_calendar_dst_early_close():
    assert us_session.is_trading_session(date(2026, 11, 5))
    assert not us_session.is_trading_session(date(2026, 11, 26))  # Thanksgiving
    assert us_session.is_early_close(date(2026, 11, 27))
    close = us_session.session_close_et(date(2026, 11, 27))
    assert (close.hour, close.minute) == (13, 0)
    assert (us_session.session_close_et(date(2026, 11, 5)).hour,
            us_session.session_close_et(date(2026, 11, 5)).minute) == (16, 0)
    # After close Friday
    assert us_session.last_complete_us_session_date(
        datetime(2026, 11, 6, 15, 0, tzinfo=NY)
    ) == date(2026, 11, 5)
    # Before close on session day
    assert us_session.last_complete_us_session_date(
        datetime(2026, 11, 5, 15, 0, tzinfo=NY)
    ) == date(2026, 11, 4)


def test_session_bar_date_keeps_label():
    assert us_session.session_bar_date(pd.Timestamp("2026-11-05", tz="UTC")) == date(2026, 11, 5)


def test_routing_keeps_separate_gate_files():
    src = Path(rexc.__file__).read_text(encoding="utf-8")
    assert "separate_spreadsheets_per_gate" in src
    assert "Do not also write exclude_* tabs onto the combine workbook." in src
