#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write per-gate + combine results to Google Sheets via service account."""
from __future__ import annotations

import json
import time
from typing import Any

# Max cells per Sheets values.update batch (~10k safe margin)
CHUNK = 4000


def _client(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(sa_json) if sa_json.lstrip().startswith("{") else None
    if info is None:
        # path to JSON file
        creds = Credentials.from_service_account_file(
            sa_json,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
    else:
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
    return gspread.authorize(creds)


def _first_worksheet(sh):
    return sh.get_worksheet(0)


def _clear_and_write(ws, rows: list[list[Any]]) -> None:
    ws.clear()
    if not rows:
        return
    # write in chunks by row
    start = 0
    while start < len(rows):
        end = min(start + CHUNK, len(rows))
        chunk = rows[start:end]
        # A1 for this chunk
        a1 = f"A{start + 1}"
        ws.update(a1, chunk, value_input_option="RAW")
        start = end
        time.sleep(0.4)


def write_all_gates(
    sa_json: str,
    cfg: dict,
    gate_hits: dict[str, list[str]],
    labels: dict[str, str],
    order: list[str],
) -> None:
    gc = _client(sa_json)
    combine_rows: list[list[str]] = [["symbol", "gate"]]
    gates_cfg = cfg.get("gates", {})

    for key in order:
        syms = sorted(set(gate_hits.get(key, [])))
        label = labels[key]
        meta = gates_cfg.get(key) or {}
        sid = meta.get("id")
        if not sid:
            print(f"Sheets: no id for {key}, skip", flush=True)
            continue
        sh = gc.open_by_key(sid)
        tab = meta.get("tab") or "hits"
        try:
            ws = sh.worksheet(tab)
        except Exception:
            ws = _first_worksheet(sh)
        rows = [["symbol"]] + [[s] for s in syms]
        print(f"Sheets: write {meta.get('title', key)} n={len(syms)}", flush=True)
        _clear_and_write(ws, rows)
        for s in syms:
            combine_rows.append([s, label])
        time.sleep(0.5)

    combine_rows[1:] = sorted(combine_rows[1:], key=lambda r: (r[0], r[1]))
    comb = cfg.get("combine") or {}
    cid = comb.get("id")
    if cid:
        sh = gc.open_by_key(cid)
        tab = comb.get("tab") or "combine"
        try:
            ws = sh.worksheet(tab)
        except Exception:
            ws = _first_worksheet(sh)
            try:
                ws.update_title(tab)
            except Exception:
                pass
        print(f"Sheets: write combine n={len(combine_rows) - 1}", flush=True)
        _clear_and_write(ws, combine_rows)
    print("Sheets: done", flush=True)
