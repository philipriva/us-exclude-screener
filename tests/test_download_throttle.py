"""Offline tests for throttled Yahoo download (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_exclude as rx  # noqa: E402


def _raw(tickers):
    idx = pd.bdate_range("2026-11-02", periods=6)
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
    return pd.DataFrame(1.0, index=idx, columns=cols)


class FakeYahoo:
    """Serves tickers unless 'blocked' (rate-limited) for the first `block_calls` calls."""

    def __init__(self, block_calls=0, dead=()):
        self.calls = 0
        self.block_calls = block_calls
        self.dead = set(dead)

    def __call__(self, bt):
        self.calls += 1
        if self.calls <= self.block_calls:
            return pd.DataFrame()
        ok = [s for s in bt if s not in self.dead]
        return _raw(ok) if ok else pd.DataFrame()


def _run(fake, syms):
    slept = []
    clock = {"t": 0.0}

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    out = rx.download(syms, fetch=fake, sleep=sleep, clock=lambda: clock["t"])
    return out, slept


def test_all_ok_single_pass():
    syms = [f"S{i}" for i in range(320)]
    out, slept = _run(FakeYahoo(), syms)
    assert set(out) == set(syms)
    assert rx.DL_COOLDOWN_BASE not in slept  # no cooldown
    assert all(s == rx.DL_PAUSE_SEC for s in slept)


def test_rate_limited_batches_recovered_by_retry():
    syms = [f"S{i}" for i in range(450)]  # 3 batches of 150
    fake = FakeYahoo(block_calls=2)
    out, slept = _run(fake, syms)
    assert set(out) == set(syms)
    assert rx.DL_COOLDOWN_BASE in slept and rx.DL_COOLDOWN_BASE * 2 in slept  # exp cooldown
    assert rx.RETRY_BACKOFF_BASE in slept  # retry round 1 backoff


def test_dead_symbols_stop_after_two_empty_rounds():
    syms = [f"S{i}" for i in range(20)] + ["DEAD1", "DEAD2"]
    out, slept = _run(FakeYahoo(dead={"DEAD1", "DEAD2"}), syms)
    assert "DEAD1" not in out and len(out) == 20
    backoffs = [s for s in slept if s >= rx.RETRY_BACKOFF_BASE]
    assert len(backoffs) == 2  # rounds 1 and 2, then stop


def test_retry_respects_budget():
    syms = [f"S{i}" for i in range(150)]
    fake = FakeYahoo(block_calls=10**9)  # never recovers
    out, slept = _run(fake, syms)
    assert out == {}
    total_retry = sum(s for s in slept if s >= rx.RETRY_BACKOFF_BASE)
    assert total_retry <= rx.RETRY_BUDGET_SEC


def test_extract_keeps_original_rules():
    raw = _raw(["A", "B"])
    raw[("B", "Close")] = float("nan")  # all-NaN close → dropped
    got = rx._extract(raw, ["A", "B"])
    assert list(got) == ["A"]
    assert {"Open", "High", "Low", "Close"} <= set(got["A"].columns)


def test_gate_constants_unchanged():
    assert (rx.N, rx.SMA, rx.BATCH) == (100, 10, 150)
    assert rx.DEFAULT_TD_D == {"S2"} and rx.DEFAULT_TD_W == {"S1", "S2"}
