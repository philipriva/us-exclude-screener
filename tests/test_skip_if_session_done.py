from datetime import date, datetime
from zoneinfo import ZoneInfo

import skip_if_session_done as m

NY = ZoneInfo("America/New_York")


def test_session_close_utc_dst_and_standard():
    assert m.session_close_utc(date(2026, 11, 9)).isoformat() == "2026-11-09T21:00:00+00:00"  # EST
    assert m.session_close_utc(date(2026, 10, 30)).isoformat() == "2026-10-30T20:00:00+00:00"  # EDT


def test_last_complete_us_session_date_rules():
    # Friday 15:59 ET → still Thursday
    assert m.last_complete_us_session_date(datetime(2026, 11, 6, 15, 59, tzinfo=NY)) == date(2026, 11, 5)
    # Friday 16:00 ET → Friday
    assert m.last_complete_us_session_date(datetime(2026, 11, 6, 16, 0, tzinfo=NY)) == date(2026, 11, 6)
    # Saturday / Sunday → Friday
    assert m.last_complete_us_session_date(datetime(2026, 11, 7, 12, 0, tzinfo=NY)) == date(2026, 11, 6)
    assert m.last_complete_us_session_date(datetime(2026, 11, 8, 9, 0, tzinfo=NY)) == date(2026, 11, 6)
    # Monday before close → Friday
    assert m.last_complete_us_session_date(datetime(2026, 11, 9, 10, 0, tzinfo=NY)) == date(2026, 11, 6)


def _run(i, ev="schedule", concl="success", ts="2026-11-09T22:05:00Z"):
    return {"id": i, "event": ev, "conclusion": concl, "created_at": ts}


def test_already_succeeded_rules():
    exp = date(2026, 11, 9)
    assert m.already_succeeded([_run(1)], exp)["id"] == 1
    assert m.already_succeeded([_run(1, ev="workflow_dispatch")], exp)["id"] == 1
    assert m.already_succeeded([_run(1, ev="workflow_run")], exp)["id"] == 1
    assert m.already_succeeded([_run(1, ev="repository_dispatch")], exp)["id"] == 1
    assert m.already_succeeded([_run(1, concl="failure")], exp) is None
    assert m.already_succeeded([_run(1, ts="2026-11-06T22:05:00Z")], exp) is None
    assert m.already_succeeded([_run(7)], exp, current_run_id=7) is None
    # CDEF-style: ignore dispatch when not in the allowed event list
    assert m.already_succeeded([_run(1, ev="workflow_dispatch")], exp, events=("schedule", "workflow_run")) is None
    assert m.already_succeeded([_run(2, ev="workflow_run")], exp, events=("schedule", "workflow_run"))["id"] == 2


def test_main_skips_and_fail_open(monkeypatch, tmp_path):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_REPOSITORY", "philipriva/ExtraScan")
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: date(2026, 11, 9))

    monkeypatch.setattr(m, "_fetch_workflow_runs", lambda *a, **k: [_run(1)])
    assert m.main(["--workflow", "daily_extrascan.yml"]) == 0
    assert "skip=true" in out.read_text()

    out.write_text("")
    monkeypatch.setattr(m, "_fetch_workflow_runs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
    assert m.main(["--workflow", "daily_extrascan.yml"]) == 0
    assert "skip=false" in out.read_text()

    out.write_text("")
    monkeypatch.setattr(m, "_fetch_workflow_runs", lambda *a, **k: [_run(1, concl="failure")])
    assert m.main(["--workflow", "daily_extrascan.yml"]) == 0
    assert "skip=false" in out.read_text()
