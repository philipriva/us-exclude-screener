#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write per-gate + combine + Duplicate results to Google Sheets via service account."""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any

# Max cells per Sheets values.update batch (~10k safe margin)
CHUNK = 4000

# Intended Sheets formula (docs only; full MAP hits calc limit → we materialize values)
DUP_FORMULA_DOC = (
    '=LET(a,combine!A2:A,b,combine!B2:B,syms,UNIQUE(FILTER(a,LEN(a)>0)),'
    'MAP(syms,LAMBDA(s,HSTACK(s,TEXTJOIN(", ",TRUE,FILTER(b,a=s)))))'
)
DUP_NOTE_DOC = (
    "Sheets calc limit on full MAP→values materialized; "
    "regenerate on each screener upload."
)


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


def _aggregate_duplicate(combine_rows: list[list[str]]) -> list[list[str]]:
    """One row per symbol (first-seen order); B = gates joined ', ' (appearance order).

    combine_rows may include header ["symbol","gate"]; data rows are [symbol, gate].
    """
    od: OrderedDict[str, list[str]] = OrderedDict()
    for row in combine_rows:
        if len(row) < 2:
            continue
        sym, gate = str(row[0]).strip(), str(row[1]).strip()
        if not sym or sym.lower() == "symbol":
            continue
        if sym not in od:
            od[sym] = []
        if gate and gate not in od[sym]:
            od[sym].append(gate)
    return [[s, ", ".join(gates)] for s, gates in od.items()]


def _ensure_worksheet(sh, title: str):
    try:
        return sh.worksheet(title)
    except Exception:
        try:
            return sh.add_worksheet(title=title, rows=10000, cols=6)
        except Exception:
            return _first_worksheet(sh)


def _write_duplicate(sh, combine_rows: list[list[str]], tab: str = "Duplicate") -> None:
    dup_rows = _aggregate_duplicate(combine_rows)
    ws = _ensure_worksheet(sh, tab)
    # Clear A:B (and leave room); then write values + docs in D
    try:
        ws.batch_clear(["A:B", "D:E"])
    except Exception:
        ws.clear()
    print(f"Sheets: write Duplicate n={len(dup_rows)}", flush=True)
    if dup_rows:
        start = 0
        while start < len(dup_rows):
            end = min(start + CHUNK, len(dup_rows))
            ws.update(f"A{start + 1}", dup_rows[start:end], value_input_option="RAW")
            start = end
            time.sleep(0.4)
    # Docs: intended formula (plain text) + note
    ws.update("D1", [[DUP_FORMULA_DOC], [DUP_NOTE_DOC]], value_input_option="RAW")
    try:
        sh.batch_update(
            {
                "requests": [
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": ws.id,
                                "dimension": "COLUMNS",
                                "startIndex": 1,
                                "endIndex": 2,
                            },
                            "properties": {"pixelSize": 520},
                            "fields": "pixelSize",
                        }
                    }
                ]
            }
        )
    except Exception as e:
        print(f"Sheets: Duplicate col width skip: {e}", flush=True)


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
        # Refresh Duplicate on same spreadsheet (us_exclude_screener)
        dup_tab = comb.get("duplicate_tab") or "Duplicate"
        _write_duplicate(sh, combine_rows, tab=dup_tab)
    print("Sheets: done", flush=True)
