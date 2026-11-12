#!/usr/bin/env python3
"""Cheap backup/schedule guard: skip this job if the same workflow already
succeeded for the latest complete US cash session.

No market download, no Sheet writes. API failure is fail-open (do not skip).
Intended as a pre-step on GitHub Actions; write skip=true|false to GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Optional

from marketdata import (
    NY,
    last_complete_us_session_date,
    ny_now,
    session_close_utc,
)

DEFAULT_EVENTS = ("schedule", "workflow_dispatch", "workflow_run", "repository_dispatch")


def already_succeeded(
    runs: list[dict],
    expected: date,
    current_run_id: Optional[int] = None,
    events: tuple[str, ...] = DEFAULT_EVENTS,
) -> Optional[dict]:
    """Return a prior successful run for ``expected``, else None.

    A conclusion=success run started after that session's 16:00 ET close, with
    an event in ``events`` and a different run id, counts as already done.
    """
    cutoff = session_close_utc(expected)
    allowed = set(events)
    for r in runs:
        if current_run_id is not None and int(r.get("id", 0) or 0) == int(current_run_id):
            continue
        if r.get("event") not in allowed or r.get("conclusion") != "success":
            continue
        ts = r.get("created_at") or r.get("run_started_at") or ""
        try:
            created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= cutoff:
            return r
    return None


def _fetch_workflow_runs(repo: str, workflow: str, token: str) -> list[dict]:
    path = workflow if "/" in workflow else workflow
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{urllib.parse.quote(path)}/runs"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = urllib.parse.urlencode({"per_page": "50"})
    req = urllib.request.Request(f"{url}?{params}", headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("workflow_runs", [])


def _emit(skip: bool, reason: str) -> None:
    print(reason, flush=True)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"skip={'true' if skip else 'false'}\n")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Skip a scheduled backup if this workflow already succeeded for the US session")
    p.add_argument("--workflow", required=True, help="Workflow file name or path, e.g. daily_extrascan.yml")
    p.add_argument(
        "--events",
        default=",".join(DEFAULT_EVENTS),
        help="Comma-separated GitHub event names that count as a real write",
    )
    args = p.parse_args(argv)
    events = tuple(e.strip() for e in args.events.split(",") if e.strip())
    expected = last_complete_us_session_date()
    current_id = int(os.environ.get("GITHUB_RUN_ID", "0") or 0)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo:
        _emit(False, "skip check: GITHUB_REPOSITORY missing; running normally")
        return 0
    try:
        runs = _fetch_workflow_runs(repo, args.workflow, token)
        done = already_succeeded(runs, expected, current_id or None, events)
    except Exception as exc:
        _emit(False, f"skip check failed ({exc}); running normally")
        return 0
    if done is not None:
        _emit(
            True,
            f"SKIP: run {done.get('id')} event={done.get('event')} already succeeded "
            f"for session {expected} (created {done.get('created_at')})",
        )
        return 0
    _emit(False, f"skip check: no prior success for session {expected}; running normally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
