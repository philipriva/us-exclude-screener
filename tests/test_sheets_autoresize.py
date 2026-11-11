"""Offline tests: worksheet grid auto-grow before writes (never shrink)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sheets_writer as sw  # noqa: E402


class FakeWS:
    def __init__(self, rows, cols, title="t"):
        self.row_count, self.col_count, self.title, self.id = rows, cols, title, 1
        self.resized = []
        self.cells = {}

    def resize(self, rows=None, cols=None):
        self.resized.append((rows, cols))
        self.row_count, self.col_count = rows, cols

    def clear(self):
        self.cells = {}

    def batch_clear(self, ranges):
        self.cells = {}

    def update(self, a1, values, value_input_option=None):
        start = int(a1[1:])
        end = start + len(values) - 1
        if end > self.row_count or max(len(v) for v in values) > self.col_count:
            raise RuntimeError(f"exceeds grid limits {a1} end={end}")
        for i, v in enumerate(values):
            self.cells[start + i] = v


class FakeSh:
    def __init__(self, ws):
        self.ws = ws

    def worksheet(self, title):
        return self.ws

    def batch_update(self, body):
        pass


def test_combine_grows_beyond_36000():
    ws = FakeWS(36000, 4, "combine")
    rows = [["symbol", "gate"]] + [[f"S{i}", "g"] for i in range(39340)]
    sw._clear_and_write(ws, rows)
    assert ws.row_count == 39341 + sw.RESIZE_MARGIN_ROWS
    assert ws.cells[39341] == ["S39339", "g"]  # tail written


def test_never_shrinks_and_no_resize_when_fits():
    ws = FakeWS(50000, 26)
    sw._clear_and_write(ws, [["symbol"]] + [[f"S{i}"] for i in range(10)])
    assert ws.resized == [] and ws.row_count == 50000 and ws.col_count == 26


def test_grows_cols_only_when_needed():
    ws = FakeWS(100, 1)
    sw._clear_and_write(ws, [["a", "b", "c"]])
    assert ws.row_count == 100 and ws.col_count == 3


def test_duplicate_grows():
    ws = FakeWS(1000, 2, "Duplicate")
    combine = [["symbol", "gate"]] + [[f"S{i}", "g1"] for i in range(5000)]
    sw._write_duplicate(FakeSh(ws), combine)
    assert ws.row_count >= 5000 and ws.col_count >= 4
    assert ws.cells[5000][0] == "S4999"


def test_ensure_grid_direct():
    ws = FakeWS(10, 10)
    sw._ensure_grid(ws, 5, 5)
    assert ws.resized == []
    sw._ensure_grid(ws, 11, 5, margin=0)
    assert (ws.row_count, ws.col_count) == (11, 10)
