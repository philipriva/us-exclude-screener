"""Freshness gate: below .90 must not write Sheets."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_exclude as rx  # noqa: E402
import marketdata as md  # noqa: E402


def test_freshness_below_90_skips_sheets(monkeypatch, tmp_path):
    stale = pd.DataFrame(
        {"Open": [1], "High": [1], "Low": [1], "Close": [1], "Volume": [1]},
        index=pd.to_datetime(["2026-11-10"]),
    )
    monkeypatch.setattr(rx, "last_complete_us_session_date", lambda: date(2026, 11, 11))
    monkeypatch.setattr(rx, "last_bar_date", lambda d, now=None: date(2026, 11, 10) if d is not None else None)
    monkeypatch.setattr(rx, "download", lambda syms, **k: {s: stale for s in syms})
    monkeypatch.setattr(rx, "download_daily", lambda *a, **k: ({}, md.DownloadStats(requested=3)))
    written = []
    monkeypatch.setattr(rx, "maybe_write_sheets", lambda *a, **k: written.append(True))
    monkeypatch.setattr(rx, "write_local", lambda *a, **k: tmp_path)
    monkeypatch.setattr(rx, "eval_gates", lambda d, forming=False: {})
    monkeypatch.setattr(sys, "argv", ["run_exclude.py", "--out-dir", str(tmp_path), "--symbols", "AAA,BBB,CCC"])
    rc = rx.main()
    assert rc == 3
    assert written == []
