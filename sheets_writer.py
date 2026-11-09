#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write per-gate + combine + Duplicate results to Google Sheets via service account.

B1/B2 fixes:
- Never skip missing SA / import / gate id on the production write path.
- Never fall back to worksheet(0); named tab missing → create-by-name or fail.
- Persist sheets_receipt.json per target with payload SHA256 + full readback.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

# Max cells per Sheets values.update batch (~10k safe margin)
CHUNK = 4000

DUP_FORMULA_DOC = (
    '=LET(a,combine!A2:A,b,combine!B2:B,syms,UNIQUE(FILTER(a,LEN(a)>0)),'
    'MAP(syms,LAMBDA(s,HSTACK(s,TEXTJOIN(", ",TRUE,FILTER(b,a=s)))))'
)
DUP_NOTE_DOC = (
    "Sheets calc limit on full MAP→values materialized; "
    "regenerate on each screener upload. "
    "B sorted by Philip priority: 月線主力>週線主力>月線SAR>MCDX>…"
)


class SheetsWriteError(RuntimeError):
    """FAIL_CLOSED Sheets write / preflight / readback failure."""


def _client(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(sa_json) if sa_json.lstrip().startswith("{") else None
    if info is None:
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


def normalize_cell_array(rows: list[list[Any]]) -> list[list[str]]:
    """Preserve RAW ticker / internal spaces; only drop trailing empty cells/rows."""
    out: list[list[str]] = []
    for row in rows or []:
        r = [("" if c is None else str(c)) for c in row]
        while r and r[-1] == "":
            r.pop()
        out.append(r)
    while out and out[-1] == []:
        out.pop()
    return out


def cell_array_sha256(rows: list[list[Any]]) -> str:
    norm = normalize_cell_array(rows)
    blob = json.dumps(norm, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


DEFAULT_DUP_PRIORITY = [
    "月線主力能量",
    "週線主力能量",
    "月線 SAR 喺價上",
    "日線 MCDX 籌碼",
    "月線 KDJ 黃柱",
    "週線 KDJ 黃柱",
    "市值 < 100M",
    "日線 close < EMA200",
    "日線 close < EMA50",
    "週線 TD Sequential",
    "日線 TD Sequential",
]


def _sort_gates(gates: list[str], priority: list[str] | None = None) -> list[str]:
    pri = priority or DEFAULT_DUP_PRIORITY
    rank = {g: i for i, g in enumerate(pri)}
    return sorted(gates, key=lambda g: (rank.get(g, 10_000), g))


def _aggregate_duplicate(
    combine_rows: list[list[str]],
    priority: list[str] | None = None,
) -> list[list[str]]:
    od: OrderedDict[str, list[str]] = OrderedDict()
    for row in combine_rows:
        if len(row) < 2:
            continue
        sym, gate = str(row[0]), str(row[1])
        # Do not strip internal spaces; only reject fully-empty / header label.
        if not sym.strip() or sym.strip().lower() == "symbol":
            continue
        if sym not in od:
            od[sym] = []
        if gate and gate not in od[sym]:
            od[sym].append(gate)
    return [[s, ", ".join(_sort_gates(gates, priority))] for s, gates in od.items()]


def resolve_worksheet(
    sh,
    title: str,
    *,
    expected_gid: int | None = None,
    allow_create: bool = False,
):
    """Resolve worksheet by exact title. NEVER fall back to worksheet(0).

    Missing tab: create under the exact title when allow_create, else fail.
    If expected_gid is set for an *existing* named tab and ids differ, fail-closed.
    Freshly created tabs skip gid match (new id is recorded in the receipt).
    """
    if not title or not str(title).strip():
        raise SheetsWriteError("FAIL_CLOSED: empty worksheet title")
    title = str(title)
    created = False
    try:
        ws = sh.worksheet(title)
    except Exception as e:
        if not allow_create:
            raise SheetsWriteError(
                f"FAIL_CLOSED: worksheet {title!r} not found and create not allowed: {e}"
            ) from e
        try:
            ws = sh.add_worksheet(title=title, rows=10000, cols=6)
            created = True
        except Exception as e2:
            raise SheetsWriteError(
                f"FAIL_CLOSED: cannot create worksheet {title!r}: {e2}"
            ) from e2
    if (
        expected_gid is not None
        and not created
        and int(ws.id) != int(expected_gid)
    ):
        raise SheetsWriteError(
            f"FAIL_CLOSED: worksheet {title!r} gid={ws.id} != expected {expected_gid}"
        )
    return ws


def _clear_and_write(ws, rows: list[list[Any]]) -> None:
    ws.clear()
    if not rows:
        return
    start = 0
    while start < len(rows):
        end = min(start + CHUNK, len(rows))
        chunk = rows[start:end]
        ws.update(f"A{start + 1}", chunk, value_input_option="RAW")
        start = end
        time.sleep(0.4)


def _readback_values(ws, n_rows: int, n_cols: int) -> list[list[Any]]:
    """Read back a range covering expected payload + one spare row for residue check."""
    rows = max(n_rows + 1, 1)
    cols = max(n_cols, 1)
    # Column letter helper (A..Z only enough for our schemas <= 5 cols; extend if needed)
    def col_letter(n: int) -> str:
        s = ""
        while n:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s or "A"

    end = f"{col_letter(cols)}{rows}"
    try:
        vals = ws.get(f"A1:{end}", value_render_option="UNFORMATTED_VALUE")
    except TypeError:
        vals = ws.get(f"A1:{end}")
    return vals or []


def list_targets_from_config(cfg: dict, order: list[str]) -> list[dict]:
    """Return the 13 production targets (11 gates + combine + Duplicate)."""
    targets: list[dict] = []
    gates_cfg = cfg.get("gates") or {}
    for key in order:
        meta = gates_cfg.get(key) or {}
        targets.append(
            {
                "key": f"gate_{key}",
                "kind": "gate",
                "gate_key": key,
                "spreadsheet_id": meta.get("id"),
                "tab": meta.get("tab"),
                "worksheet_gid": meta.get("worksheet_gid"),
                "title": meta.get("title") or key,
                "schema": ["symbol"],
                "allow_create": False,
            }
        )
    comb = cfg.get("combine") or {}
    targets.append(
        {
            "key": "combine",
            "kind": "combine",
            "spreadsheet_id": comb.get("id"),
            "tab": comb.get("tab") or "combine",
            "worksheet_gid": comb.get("worksheet_gid"),
            "title": comb.get("title") or "us_exclude_screener",
            "schema": ["symbol", "gate"],
            "allow_create": False,
        }
    )
    targets.append(
        {
            "key": "Duplicate",
            "kind": "duplicate",
            "spreadsheet_id": comb.get("id"),
            "tab": comb.get("duplicate_tab") or "Duplicate",
            "worksheet_gid": comb.get("duplicate_worksheet_gid"),
            "title": comb.get("title") or "us_exclude_screener",
            "schema": ["symbol", "gates_joined"],
            "allow_create": True,  # may create named Duplicate tab only — never sheet 0
        }
    )
    return targets


def preflight_sheet_targets(cfg: dict, order: list[str]) -> list[dict]:
    """Validate all 13 targets have spreadsheet id + explicit tab before any write."""
    targets = list_targets_from_config(cfg, order)
    errors: list[str] = []
    expected_n = len(order) + 2  # gates + combine + Duplicate
    if len(targets) != expected_n:
        errors.append(f"expected {expected_n} targets, got {len(targets)}")
    for t in targets:
        if not t.get("spreadsheet_id"):
            errors.append(f"{t['key']}: missing spreadsheet_id")
        if not t.get("tab"):
            errors.append(f"{t['key']}: missing explicit tab name")
    if errors:
        raise SheetsWriteError(
            "FAIL_CLOSED preflight: " + "; ".join(errors)
        )
    return targets


def _save_receipt(path: Path | None, receipt: dict) -> None:
    if path is None:
        return
    _atomic_write_json(path, receipt)


def write_all_gates(
    sa_json: str,
    cfg: dict,
    gate_hits: dict[str, list[str]],
    labels: dict[str, str],
    order: list[str],
    dup_priority: list[str] | None = None,
    *,
    receipt_path: Path | str | None = None,
) -> dict:
    """Write all 13 targets; return receipt. Raises SheetsWriteError on any failure."""
    receipt_path = Path(receipt_path) if receipt_path else None
    receipt: dict[str, Any] = {
        "status": "in_progress",
        "sheets_write_attempted": True,
        "sheets_write_success": False,
        "targets": {},
        "errors": [],
    }
    _save_receipt(receipt_path, receipt)

    try:
        targets_meta = preflight_sheet_targets(cfg, order)
    except SheetsWriteError as e:
        receipt["status"] = "failed_preflight"
        receipt["errors"].append(str(e))
        for t in list_targets_from_config(cfg, order):
            receipt["targets"][t["key"]] = {
                "status": "not_attempted",
                "spreadsheet_id": t.get("spreadsheet_id"),
                "tab": t.get("tab"),
                "error": str(e),
            }
        _save_receipt(receipt_path, receipt)
        raise

    for t in targets_meta:
        receipt["targets"][t["key"]] = {
            "status": "pending",
            "spreadsheet_id": t["spreadsheet_id"],
            "tab": t["tab"],
            "expected_worksheet_gid": t.get("worksheet_gid"),
            "schema": t["schema"],
            "allow_create": t["allow_create"],
        }
    _save_receipt(receipt_path, receipt)

    try:
        gc = _client(sa_json)
    except Exception as e:
        receipt["status"] = "failed_client"
        receipt["errors"].append(f"client: {e}")
        for k, t in receipt["targets"].items():
            if t.get("status") == "pending":
                t["status"] = "not_attempted"
                t["error"] = f"client init failed: {e}"
        _save_receipt(receipt_path, receipt)
        raise SheetsWriteError(f"FAIL_CLOSED: Sheets client init failed: {e}") from e

    # Build payloads
    combine_rows: list[list[str]] = [["symbol", "gate"]]
    gate_payloads: dict[str, list[list[str]]] = {}
    for key in order:
        syms = sorted(set(gate_hits.get(key, [])))
        label = labels[key]
        rows = [["symbol"]] + [[s] for s in syms]
        gate_payloads[key] = rows
        for s in syms:
            combine_rows.append([s, label])
    combine_rows[1:] = sorted(combine_rows[1:], key=lambda r: (r[0], r[1]))
    dup_data = _aggregate_duplicate(combine_rows, priority=dup_priority)
    # Duplicate A:B values (no header historically) + docs in D1:D2
    dup_ab = dup_data
    dup_docs = [[DUP_FORMULA_DOC], [DUP_NOTE_DOC]]

    def _mark(key: str, **fields):
        receipt["targets"][key].update(fields)
        _save_receipt(receipt_path, receipt)

    try:
        # --- 11 gates ---
        for key in order:
            tkey = f"gate_{key}"
            meta = next(t for t in targets_meta if t["key"] == tkey)
            rows = gate_payloads[key]
            exp_sha = cell_array_sha256(rows)
            _mark(
                tkey,
                status="attempting",
                expected_rows=len(rows),
                expected_cols=1,
                expected_sha256=exp_sha,
            )
            sh = gc.open_by_key(meta["spreadsheet_id"])
            ws = resolve_worksheet(
                sh,
                meta["tab"],
                expected_gid=meta.get("worksheet_gid"),
                allow_create=False,
            )
            _mark(
                tkey,
                actual_tab_title=ws.title,
                actual_worksheet_gid=int(ws.id),
                write_status="writing",
            )
            _clear_and_write(ws, rows)
            time.sleep(0.5)
            rb = _readback_values(ws, len(rows), 2)
            rb_norm = normalize_cell_array(rb)
            exp_norm = normalize_cell_array(rows)
            # residue: any non-empty cell beyond expected rows
            residue = False
            if len(rb_norm) > len(exp_norm):
                residue = True
            rb_sha = cell_array_sha256(rb_norm[: len(exp_norm)])
            ok = (rb_norm[: len(exp_norm)] == exp_norm) and not residue
            _mark(
                tkey,
                write_status="written",
                readback_status="ok" if ok else "mismatch",
                readback_sha256=rb_sha,
                readback_rows=len(rb_norm),
                residue=residue,
                status="ok" if ok else "failed_readback",
                error=None if ok else "readback cell-array mismatch or residue",
            )
            if not ok:
                raise SheetsWriteError(
                    f"FAIL_CLOSED readback mismatch for {tkey}"
                )

        # --- combine ---
        cmeta = next(t for t in targets_meta if t["key"] == "combine")
        exp_sha = cell_array_sha256(combine_rows)
        _mark(
            "combine",
            status="attempting",
            expected_rows=len(combine_rows),
            expected_cols=2,
            expected_sha256=exp_sha,
        )
        sh = gc.open_by_key(cmeta["spreadsheet_id"])
        ws = resolve_worksheet(
            sh,
            cmeta["tab"],
            expected_gid=cmeta.get("worksheet_gid"),
            allow_create=False,
        )
        _mark(
            "combine",
            actual_tab_title=ws.title,
            actual_worksheet_gid=int(ws.id),
            write_status="writing",
        )
        _clear_and_write(ws, combine_rows)
        rb = _readback_values(ws, len(combine_rows), 3)
        rb_norm = normalize_cell_array(rb)
        exp_norm = normalize_cell_array(combine_rows)
        residue = len(rb_norm) > len(exp_norm)
        ok = rb_norm[: len(exp_norm)] == exp_norm and not residue
        _mark(
            "combine",
            write_status="written",
            readback_status="ok" if ok else "mismatch",
            readback_sha256=cell_array_sha256(rb_norm[: len(exp_norm)]),
            readback_rows=len(rb_norm),
            residue=residue,
            status="ok" if ok else "failed_readback",
            error=None if ok else "readback cell-array mismatch or residue",
        )
        if not ok:
            raise SheetsWriteError("FAIL_CLOSED readback mismatch for combine")

        # --- Duplicate ---
        dmeta = next(t for t in targets_meta if t["key"] == "Duplicate")
        # Expected A:B payload for hash (docs in D verified separately)
        exp_sha = cell_array_sha256(dup_ab)
        _mark(
            "Duplicate",
            status="attempting",
            expected_rows=len(dup_ab),
            expected_cols=2,
            expected_sha256=exp_sha,
        )
        # Same spreadsheet as combine — already opened
        ws = resolve_worksheet(
            sh,
            dmeta["tab"],
            expected_gid=dmeta.get("worksheet_gid"),
            allow_create=True,
        )
        if ws.title != dmeta["tab"]:
            raise SheetsWriteError("FAIL_CLOSED Duplicate title mismatch after resolve")
        _mark(
            "Duplicate",
            actual_tab_title=ws.title,
            actual_worksheet_gid=int(ws.id),
            write_status="writing",
        )
        try:
            ws.batch_clear(["A:B", "D:E"])
        except Exception:
            ws.clear()
        if dup_ab:
            start = 0
            while start < len(dup_ab):
                end = min(start + CHUNK, len(dup_ab))
                ws.update(f"A{start + 1}", dup_ab[start:end], value_input_option="RAW")
                start = end
                time.sleep(0.4)
        ws.update("D1", dup_docs, value_input_option="RAW")
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

        # Readback A:B
        rb = _readback_values(ws, max(len(dup_ab), 1), 3)
        rb_ab = [r[:2] for r in rb]
        rb_norm = normalize_cell_array(rb_ab)
        exp_norm = normalize_cell_array(dup_ab)
        residue = len(rb_norm) > len(exp_norm)
        ok_ab = rb_norm[: len(exp_norm)] == exp_norm and not residue
        # Docs D1:D2
        try:
            dvals = ws.get("D1:D2", value_render_option="UNFORMATTED_VALUE") or []
        except TypeError:
            dvals = ws.get("D1:D2") or []
        d_norm = normalize_cell_array(dvals)
        ok_docs = d_norm == normalize_cell_array(dup_docs)
        ok = ok_ab and ok_docs
        _mark(
            "Duplicate",
            write_status="written",
            readback_status="ok" if ok else "mismatch",
            readback_sha256=cell_array_sha256(rb_norm[: len(exp_norm)]),
            readback_rows=len(rb_norm),
            residue=residue,
            docs_ok=ok_docs,
            status="ok" if ok else "failed_readback",
            error=None if ok else "readback mismatch (A:B and/or D docs) or residue",
        )
        if not ok:
            raise SheetsWriteError("FAIL_CLOSED readback mismatch for Duplicate")

    except Exception as e:
        receipt["status"] = "failed"
        receipt["sheets_write_success"] = False
        receipt["errors"].append(str(e))
        for k, t in receipt["targets"].items():
            if t.get("status") in ("pending", "attempting"):
                t["status"] = "not_completed"
                t.setdefault("error", str(e))
        _save_receipt(receipt_path, receipt)
        if isinstance(e, SheetsWriteError):
            raise
        raise SheetsWriteError(f"FAIL_CLOSED Sheets write: {e}") from e

    # Final: all 13 must be ok
    bad = [k for k, t in receipt["targets"].items() if t.get("status") != "ok"]
    expected_n = len(order) + 2
    if bad or len(receipt["targets"]) != expected_n:
        receipt["status"] = "failed"
        receipt["sheets_write_success"] = False
        receipt["errors"].append(f"incomplete targets: {bad}")
        _save_receipt(receipt_path, receipt)
        raise SheetsWriteError(
            f"FAIL_CLOSED: not all {expected_n} targets ok: {bad}"
        )

    receipt["status"] = "success"
    receipt["sheets_write_success"] = True
    _save_receipt(receipt_path, receipt)
    print(f"Sheets: done ({expected_n}/{expected_n} readback ok)", flush=True)
    return receipt
