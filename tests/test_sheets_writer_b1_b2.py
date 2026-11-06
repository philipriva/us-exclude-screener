#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1/B2: production Sheets path fail-closed + no worksheet(0) fallback."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sheets_writer as sw
import run_exclude as rexc


class WS:
    def __init__(self, title, gid=1):
        self.title = title
        self.id = gid
        self.writes = []
        self._data = []

    def clear(self):
        self._data = []

    def batch_clear(self, ranges):
        self._data = []

    def update(self, a1, values=None, **k):
        if values is None and isinstance(a1, list):
            values = a1
            a1 = "A1"
        self.writes.append((a1, values))
        # naive store for get()
        if a1.startswith("A") and a1[1:].isdigit():
            start = int(a1[1:]) - 1
            while len(self._data) < start + len(values):
                self._data.append([])
            for i, row in enumerate(values):
                self._data[start + i] = list(row)
        elif a1 == "D1":
            self._docs = values

    def get(self, rng, **k):
        if rng.startswith("D"):
            return getattr(self, "_docs", [])
        # return stored A rows; pad for residue check
        return [list(r) for r in self._data]

    def update_title(self, t):
        self.title = t


class SH:
    def __init__(self, tabs=None):
        self._tabs = dict(tabs or {})
        self.batch_calls = 0

    def worksheet(self, title):
        if title not in self._tabs:
            raise Exception(f"no tab {title}")
        return self._tabs[title]

    def add_worksheet(self, title=None, rows=1000, cols=6):
        if title in self._tabs:
            raise Exception("exists")
        ws = WS(title, gid=99)
        self._tabs[title] = ws
        return ws

    def get_worksheet(self, i):
        # Should never be used by fixed writer
        raise AssertionError("get_worksheet(0) fallback must not be called")

    def batch_update(self, body):
        self.batch_calls += 1


class GC:
    def __init__(self):
        self.sh = {}

    def open_by_key(self, key):
        return self.sh.setdefault(key, SH())


ORDER = ["g1", "g2"]
LABELS = {"g1": "L1", "g2": "L2"}


def _full_cfg():
    return {
        "gates": {
            "g1": {"id": "A", "tab": "exclude_01", "worksheet_gid": 1, "title": "exclude_01"},
            "g2": {"id": "B", "tab": "exclude_02", "worksheet_gid": 2, "title": "exclude_02"},
        },
        "combine": {
            "id": "C",
            "tab": "combine",
            "worksheet_gid": 10,
            "duplicate_tab": "Duplicate",
            "duplicate_worksheet_gid": 11,
            "title": "main",
        },
    }


def test_bug_repro_missing_gate_id_no_longer_done(monkeypatch, capsys, tmp_path):
    """Independent review repro must FAIL after fix (was PASS while buggy)."""
    gc = GC()
    monkeypatch.setattr(sw, "_client", lambda j: gc)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    cfg = {"gates": {"g1": {"id": "A", "tab": "exclude_01"}}, "combine": {"id": "C", "tab": "combine"}}
    with pytest.raises(sw.SheetsWriteError, match="preflight|missing"):
        sw.write_all_gates("{}", cfg, {"g1": ["X"], "g2": ["Y"]}, LABELS, ORDER, receipt_path=tmp_path / "r.json")
    out = capsys.readouterr().out
    assert "Sheets: done" not in out


def test_duplicate_missing_create_fail_does_not_touch_combine(monkeypatch, tmp_path):
    gc = GC()
    # Main sheet has combine only; Duplicate create fails
    main = SH({"combine": WS("combine", gid=10)})

    def add_fail(**k):
        raise Exception("no perm")

    main.add_worksheet = add_fail  # type: ignore
    gc.sh["C"] = main
    # Gate sheets present
    gc.sh["A"] = SH({"exclude_01": WS("exclude_01", gid=1)})
    gc.sh["B"] = SH({"exclude_02": WS("exclude_02", gid=2)})

    monkeypatch.setattr(sw, "_client", lambda j: gc)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    cfg = _full_cfg()
    with pytest.raises(sw.SheetsWriteError, match="Duplicate|cannot create"):
        sw.write_all_gates(
            "{}",
            cfg,
            {"g1": ["X"], "g2": ["Y"]},
            LABELS,
            ORDER,
            receipt_path=tmp_path / "r.json",
        )
    # combine must not have received Duplicate D1 docs via first worksheet
    assert not any(
        a1 == "D1" for a1, _ in main._tabs["combine"].writes
    )
    receipt = json.loads((tmp_path / "r.json").read_text())
    assert receipt["sheets_write_success"] is False
    assert receipt["targets"]["Duplicate"]["status"] != "ok"


def test_gate_missing_tab_no_fallback(monkeypatch, tmp_path):
    gc = GC()
    # Gate A missing named tab; has unrelated first tab
    bad = SH({"Sheet1": WS("Sheet1", gid=0)})
    gc.sh["A"] = bad
    gc.sh["B"] = SH({"exclude_02": WS("exclude_02", gid=2)})
    gc.sh["C"] = SH({"combine": WS("combine", gid=10), "Duplicate": WS("Duplicate", gid=11)})
    monkeypatch.setattr(sw, "_client", lambda j: gc)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    with pytest.raises(sw.SheetsWriteError, match="exclude_01"):
        sw.write_all_gates(
            "{}",
            _full_cfg(),
            {"g1": ["X"], "g2": []},
            LABELS,
            ORDER,
            receipt_path=tmp_path / "r.json",
        )
    with pytest.raises(AssertionError):
        bad.get_worksheet(0)


def test_readback_count_same_content_diff_fails(monkeypatch, tmp_path):
    gc = GC()

    class LyingWS(WS):
        def get(self, rng, **k):
            # same row count, different ticker
            if rng.startswith("D"):
                return [[sw.DUP_FORMULA_DOC], [sw.DUP_NOTE_DOC]]
            return [["symbol"], ["NOT_X"]]

    g1 = LyingWS("exclude_01", gid=1)
    gc.sh["A"] = SH({"exclude_01": g1})
    gc.sh["B"] = SH({"exclude_02": WS("exclude_02", gid=2)})
    gc.sh["C"] = SH({"combine": WS("combine", gid=10), "Duplicate": WS("Duplicate", gid=11)})
    monkeypatch.setattr(sw, "_client", lambda j: gc)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    with pytest.raises(sw.SheetsWriteError, match="readback"):
        sw.write_all_gates(
            "{}",
            _full_cfg(),
            {"g1": ["X"], "g2": []},
            LABELS,
            ORDER,
            receipt_path=tmp_path / "r.json",
        )
    receipt = json.loads((tmp_path / "r.json").read_text())
    assert receipt["targets"]["gate_g1"]["status"] == "failed_readback"


def test_residue_row_fails(monkeypatch, tmp_path):
    gc = GC()

    class ResidueWS(WS):
        def get(self, rng, **k):
            if rng.startswith("D"):
                return [[sw.DUP_FORMULA_DOC], [sw.DUP_NOTE_DOC]]
            # expected 2 rows but leftover third
            return [["symbol"], ["X"], ["OLD"]]

    g1 = ResidueWS("exclude_01", gid=1)
    gc.sh["A"] = SH({"exclude_01": g1})
    gc.sh["B"] = SH({"exclude_02": WS("exclude_02", gid=2)})
    gc.sh["C"] = SH({"combine": WS("combine", gid=10), "Duplicate": WS("Duplicate", gid=11)})
    monkeypatch.setattr(sw, "_client", lambda j: gc)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    with pytest.raises(sw.SheetsWriteError, match="readback"):
        sw.write_all_gates(
            "{}",
            _full_cfg(),
            {"g1": ["X"], "g2": []},
            LABELS,
            ORDER,
            receipt_path=tmp_path / "r.json",
        )


def test_mid_write_failure_receipt_keeps_partial(monkeypatch, tmp_path):
    gc = GC()
    g1 = WS("exclude_01", gid=1)
    g2 = WS("exclude_02", gid=2)

    def boom_clear():
        raise RuntimeError("boom mid-write")

    g2.clear = boom_clear  # type: ignore
    gc.sh["A"] = SH({"exclude_01": g1})
    gc.sh["B"] = SH({"exclude_02": g2})
    gc.sh["C"] = SH({"combine": WS("combine", gid=10), "Duplicate": WS("Duplicate", gid=11)})
    monkeypatch.setattr(sw, "_client", lambda j: gc)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    with pytest.raises(sw.SheetsWriteError):
        sw.write_all_gates(
            "{}",
            _full_cfg(),
            {"g1": ["X"], "g2": ["Y"]},
            LABELS,
            ORDER,
            receipt_path=tmp_path / "r.json",
        )
    receipt = json.loads((tmp_path / "r.json").read_text())
    assert receipt["targets"]["gate_g1"]["status"] == "ok"
    assert receipt["targets"]["gate_g2"]["status"] != "ok"
    assert receipt["targets"]["combine"]["status"] in ("pending", "not_completed", "attempting")
    assert receipt["sheets_write_success"] is False


def test_production_missing_sa_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    cfg = _full_cfg()
    (tmp_path / "sheet_ids.json").write_text(json.dumps(cfg))
    with pytest.raises(rexc.SheetsConfigError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        rexc.maybe_write_sheets(
            {"g1": [], "g2": []},
            tmp_path / "sheet_ids.json",
            out_dir=tmp_path,
            ready_for_sheets_prewrite=True,
        )
    receipt = json.loads((tmp_path / "sheets_receipt.json").read_text())
    assert receipt["sheets_write_success"] is False
    assert receipt["sheets_write_attempted"] is False


def test_skip_sheets_is_explicit_local_only(tmp_path, monkeypatch):
    monkeypatch.setattr(rexc, "universe", lambda: ["AAA"])
    monkeypatch.setattr(
        rexc,
        "download",
        lambda syms, **k: (
            {"AAA": __import__("pandas").DataFrame(
                {"Open": [1], "High": [1], "Low": [1], "Close": [1]},
                index=__import__("pandas").bdate_range(end="2026-11-05", periods=30),
            )},
            {},
        ),
    )
    monkeypatch.setattr(rexc, "last_complete_us_session_date", lambda: __import__("datetime").date(2026, 11, 5))
    monkeypatch.setattr(rexc, "eval_gates", lambda *a, **k: {g: False for g in rexc.GATE_ORDER if g != "04_mcap"})
    monkeypatch.setattr(rexc, "maybe_write_sheets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))
    monkeypatch.setattr(sys, "argv", [
        "run_exclude.py", "--out-dir", str(tmp_path), "--gates", "02_ema200", "--skip-sheets",
        "--symbols", "AAA",
    ])
    rc = rexc.main()
    assert rc == 0
    receipt = json.loads((tmp_path / "sheets_receipt.json").read_text())
    assert receipt["status"] == "skipped_explicit"
    meta = json.loads((tmp_path / "run_outcome.json").read_text())
    assert meta["sheets_write_success"] is False
    assert meta["ready_for_sheets_prewrite"] is True
    assert meta["sheets_receipt_status"] == "skipped_explicit"


def test_preflight_requires_all_thirteen():
    cfg = _full_cfg()
    # Only 2 gates in ORDER for unit convenience — production GATE_ORDER has 11.
    # Use real GATE_ORDER length check via list_targets_from_config on real keys.
    targets = sw.list_targets_from_config(
        json.loads((ROOT / "sheet_ids.json").read_text()),
        rexc.GATE_ORDER,
    )
    assert len(targets) == 13
    assert {t["key"] for t in targets} == (
        {f"gate_{k}" for k in rexc.GATE_ORDER} | {"combine", "Duplicate"}
    )
    for t in targets:
        assert t["spreadsheet_id"]
        assert t["tab"]
