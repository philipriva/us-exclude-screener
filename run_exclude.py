#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions / CLI: 唔考慮篩選 — 11 gates run separately → Sheets + combine.

Formulas match us_exclude_screener_app.pyw (defaults):
1. 日線 MCDX 籌碼 — SMA10(PC)<SMA10(LC)
2. 日線 close < EMA200
3. 日線 close < EMA50
4. 市值 < 100M (unknown → skip)
5. 日線 TD Sequential — S2 only
6. 週線 TD Sequential — S1+S2
7. 週線主力能量 — 做空 > 做多
8. 週線 KDJ 黃柱 — J crossunder 50
9. 月線 KDJ 黃柱 — completed month only
10. 月線 SAR 喺價上 — TV ta.sar; include forming month
11. 月線主力能量 — 做空 > 做多 (same Tongdaxin as weekly; completed month)

Universe: Nasdaq Trader US incl. ETF/Fund. Yahoo auto_adjust=True.
Default: drop forming daily/weekly bars (gates 1–8); gates 9+11 completed month;
gate 10 includes forming month.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
N, SMA, BATCH = 100, 10, 150
MCAP_MAX = 100_000_000
MCAP_WORKERS = 8
DEFAULT_TD_D = {"S2"}
DEFAULT_TD_W = {"S1", "S2"}
PERIOD = "5y"

GATE_ORDER = [
    "01_mcdx",
    "02_ema200",
    "03_ema50",
    "04_mcap",
    "05_td_daily",
    "06_td_weekly",
    "07_zhuli",
    "08_kdj_weekly",
    "09_kdj_monthly",
    "10_sar_monthly",
    "11_zhuli_monthly",
]

GATE_LABELS = {
    "01_mcdx": "日線 MCDX 籌碼",
    "02_ema200": "日線 close < EMA200",
    "03_ema50": "日線 close < EMA50",
    "04_mcap": "市值 < 100M",
    "05_td_daily": "日線 TD Sequential",
    "06_td_weekly": "週線 TD Sequential",
    "07_zhuli": "週線主力能量",
    "08_kdj_weekly": "週線 KDJ 黃柱",
    "09_kdj_monthly": "月線 KDJ 黃柱",
    "10_sar_monthly": "月線 SAR 喺價上",
    "11_zhuli_monthly": "月線主力能量",
}

# Duplicate tab B-column join order (1 = leftmost / highest priority)
DUP_GATE_PRIORITY = [
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


def _dir(url: str) -> pd.DataFrame:
    t = requests.get(url, timeout=60).text
    t = "\n".join(l for l in t.splitlines() if l and not l.startswith("File Creation"))
    return pd.read_csv(io.StringIO(t), sep="|", dtype=str)


def universe() -> list[str]:
    a = _dir(NASDAQ_LISTED).rename(columns={"Symbol": "sym"})
    b = _dir(OTHER_LISTED).rename(columns={"ACT Symbol": "sym"})
    pieces = []
    for df in (a, b):
        use = [c for c in ("sym", "Test Issue") if c in df.columns]
        pieces.append(df[use])
    df = pd.concat(pieces).dropna(subset=["sym"])
    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].fillna("N") == "N"]
    df = df[~df.sym.str.contains(r"[\$\^]")]
    return sorted({s.strip().upper().replace(".", "-") for s in df.sym})


def parse_syms(text: str) -> list[str]:
    out, seen = [], set()
    for p in text.replace(",", " ").split():
        s = p.strip().upper().replace(".", "-")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def ny_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow() - timedelta(hours=4)


def drop_forming(d: pd.DataFrame) -> pd.DataFrame:
    now = ny_now()
    last = pd.Timestamp(d.index[-1]).date()
    if last == now.date() and (now.hour, now.minute) < (16, 15):
        return d.iloc[:-1]
    return d


def drop_forming_weekly(w: pd.DataFrame) -> pd.DataFrame:
    if w is None or w.empty:
        return w
    now = ny_now()
    last = pd.Timestamp(w.index[-1]).normalize()
    fri_close = datetime(last.year, last.month, last.day, 16, 15)
    if now < fri_close:
        return w.iloc[:-1]
    return w


def drop_forming_monthly(m: pd.DataFrame) -> pd.DataFrame:
    if m is None or m.empty:
        return m
    now = ny_now()
    last = pd.Timestamp(m.index[-1])
    month_end = (last + pd.offsets.MonthEnd(0)).date()
    if now.date() < month_end:
        return m.iloc[:-1] if len(m) > 1 else m
    if now.date() == month_end and (now.hour, now.minute) < (16, 15):
        return m.iloc[:-1] if len(m) > 1 else m
    return m


def to_weekly(d: pd.DataFrame) -> pd.DataFrame:
    cols = {}
    for name, how in (
        ("Open", "first"),
        ("High", "max"),
        ("Low", "min"),
        ("Close", "last"),
        ("Volume", "sum"),
    ):
        if name in d.columns:
            cols[name] = how
    if "Close" not in cols:
        return pd.DataFrame()
    return d.resample("W-FRI").agg(cols).dropna(subset=["Close"])


def to_monthly(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x.index = pd.to_datetime(x.index)
    cols = {}
    for name, how in (
        ("Open", "first"),
        ("High", "max"),
        ("Low", "min"),
        ("Close", "last"),
        ("Volume", "sum"),
    ):
        if name in x.columns:
            cols[name] = how
    if "Close" not in cols:
        return pd.DataFrame()
    need = ["Close"]
    if "High" in cols and "Low" in cols:
        need = ["Close", "High", "Low"]
    return x.resample("ME").agg(cols).dropna(subset=need)


def sma_cn(src: pd.Series, length: int) -> pd.Series:
    vals = src.astype(float).tolist()
    out: list[float] = []
    y = float("nan")
    for v in vals:
        if pd.isna(v):
            out.append(float("nan"))
            continue
        if pd.isna(y):
            y = float(v)
        else:
            y = (float(v) + (length - 1) * y) / length
        out.append(y)
    return pd.Series(out, index=src.index)


def kdj_j(df: pd.DataFrame) -> pd.Series:
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    c = df["Close"].astype(float)
    llv = l.rolling(9, min_periods=9).min()
    hhv = h.rolling(9, min_periods=9).max()
    rng = hhv - llv
    rsv = ((c - llv) / rng.replace(0, pd.NA) * 100).fillna(0)
    k = sma_cn(rsv, 3)
    d = sma_cn(k, 3)
    return 3 * k - 2 * d


def kdj_yellow(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 15:
        return False
    j = kdj_j(df)
    if len(j) < 2:
        return False
    prev, curr = j.iloc[-2], j.iloc[-1]
    if pd.isna(prev) or pd.isna(curr):
        return False
    return float(prev) >= 50.0 and float(curr) < 50.0


def parabolic_sar(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    start: float = 0.02,
    increment: float = 0.02,
    maximum: float = 0.20,
) -> pd.Series:
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    n = len(h)
    sar = np.full(n, np.nan)
    if n < 2:
        return pd.Series(sar, index=high.index, name="SAR")
    up = bool(c[1] >= c[0])
    af = float(start)
    if up:
        sar[1] = l[0]
        ep = float(h[1])
    else:
        sar[1] = h[0]
        ep = float(l[1])
    for i in range(2, n):
        cur = float(sar[i - 1] + af * (ep - sar[i - 1]))
        reversed_ = False
        if up:
            if l[i] < cur:
                up = False
                cur = ep
                ep = float(l[i])
                af = float(start)
                reversed_ = True
        else:
            if h[i] > cur:
                up = True
                cur = ep
                ep = float(h[i])
                af = float(start)
                reversed_ = True
        if not reversed_:
            if up:
                cur = min(cur, float(l[i - 1]), float(l[i - 2]))
                if h[i] > ep:
                    ep = float(h[i])
                    af = min(af + float(increment), float(maximum))
            else:
                cur = max(cur, float(h[i - 1]), float(h[i - 2]))
                if l[i] < ep:
                    ep = float(l[i])
                    af = min(af + float(increment), float(maximum))
        sar[i] = cur
    return pd.Series(sar, index=high.index, name="SAR")


def monthly_sar_above_close(m: pd.DataFrame) -> bool:
    if m is None or len(m) < 3:
        return False
    try:
        c, h, l = _ohlc(m)
    except KeyError:
        return False
    sar = parabolic_sar(h, l, c)
    if pd.isna(sar.iloc[-1]) or pd.isna(c.iloc[-1]):
        return False
    return float(c.iloc[-1]) < float(sar.iloc[-1])


def zhuli_bear_gt_bull(w: pd.DataFrame) -> bool:
    if w is None or w.empty or len(w) < 40:
        return False
    try:
        c, h, l = _ohlc(w)
    except KeyError:
        return False
    hh36 = h.rolling(36, min_periods=36).max()
    ll36 = l.rolling(36, min_periods=36).min()
    rng36 = hh36 - ll36
    var1 = ((hh36 - c) / rng36 * 100.0).where(rng36 > 0, 0.0)
    bear = sma_cn(var1, 3)
    hh9 = h.rolling(9, min_periods=9).max()
    ll9 = l.rolling(9, min_periods=9).min()
    rng9 = hh9 - ll9
    var2 = ((c - ll9) / rng9 * 100.0).where(rng9 > 0, 0.0)
    bull = sma_cn(var2, 5) - 8.0
    b, u = float(bear.iloc[-1]), float(bull.iloc[-1])
    if pd.isna(b) or pd.isna(u):
        return False
    return b > u


def fmcdx(price, h, l, n=N):
    lo = l.rolling(n, min_periods=n).min()
    hi = h.rolling(n, min_periods=n).max()
    rng = hi - lo
    step = (rng / 100.0).where(rng > 0, 1e-8)
    return ((price - lo) / step).clip(0, 100)


def _ohlc(d):
    if isinstance(d.columns, pd.MultiIndex):
        d = d.copy()
        d.columns = d.columns.get_level_values(0)
    cols = {str(c).lower(): c for c in d.columns}
    for need in ("close", "high", "low"):
        if need not in cols:
            raise KeyError(need)
    return (
        pd.to_numeric(d[cols["close"]], errors="coerce"),
        pd.to_numeric(d[cols["high"]], errors="coerce"),
        pd.to_numeric(d[cols["low"]], errors="coerce"),
    )


def td_setup_counts(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Return (red/S count, green/B count). S=RED=close<close[4]."""
    c = close.astype(float)
    b_count = pd.Series(0, index=c.index, dtype=int)
    s_count = pd.Series(0, index=c.index, dtype=int)
    bc = sc = 0
    vals = c.tolist()
    for i in range(len(vals)):
        if i < 4 or pd.isna(vals[i]) or pd.isna(vals[i - 4]):
            bc = 0
            sc = 0
        else:
            bc = bc + 1 if vals[i] < vals[i - 4] else 0
            sc = sc + 1 if vals[i] > vals[i - 4] else 0
        b_count.iloc[i] = bc
        s_count.iloc[i] = sc
    return b_count, s_count


def td_match(df: pd.DataFrame, codes: set[str]) -> bool:
    if not codes or df is None or df.empty or "Close" not in df.columns:
        return False
    red_count, green_count = td_setup_counts(df["Close"])
    red, green = int(red_count.iloc[-1]), int(green_count.iloc[-1])
    for code in codes:
        if not code or len(code) < 2:
            continue
        side, num_s = code[0].upper(), code[1:]
        try:
            num = int(num_s)
        except ValueError:
            continue
        if side == "S" and red == num:
            return True
        if side == "B" and green == num:
            return True
    return False


def fetch_mcap(sym: str) -> float | None:
    try:
        t = yf.Ticker(sym)
        for getter in (
            lambda: getattr(t.fast_info, "market_cap", None),
            lambda: (t.info or {}).get("marketCap"),
        ):
            try:
                v = getter()
                if v is not None and float(v) > 0:
                    return float(v)
            except Exception:
                pass
    except Exception:
        pass
    return None


def download(syms: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    n = len(syms)
    for i in range(0, n, BATCH):
        bt = syms[i : i + BATCH]
        print(f"download {min(i + BATCH, n)}/{n}", flush=True)
        raw = None
        for a in range(3):
            try:
                raw = yf.download(
                    bt,
                    period=PERIOD,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    threads=True,
                    progress=False,
                )
                break
            except Exception as e:
                print("retry", a, e, flush=True)
                time.sleep(1.5 * (a + 1))
        if raw is None or raw.empty:
            continue
        for s in bt:
            try:
                d = raw[s] if len(bt) > 1 else raw
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(-1)
                # auto_adjust may drop Adj Close; need OHLC
                need = [c for c in ("Open", "High", "Low", "Close") if c in d.columns]
                if len(need) < 4:
                    continue
                d = d.dropna(subset=["Close", "High", "Low"])
                if not d.empty:
                    out[s] = d
            except Exception:
                pass
        time.sleep(0.15)
    return out


def eval_gates(d: pd.DataFrame, forming: bool = False) -> dict[str, bool]:
    """Evaluate price gates 1–3,5–11 (not mcap). Keys = GATE_ORDER without 04_mcap."""
    hits = {k: False for k in GATE_ORDER if k != "04_mcap"}
    if d is None or d.empty:
        return hits
    dd = d if forming else drop_forming(d)
    if dd is None or dd.empty or len(dd) < 5:
        return hits
    try:
        c, h, l = _ohlc(dd)
    except KeyError:
        return hits
    close = float(c.iloc[-1])
    if pd.isna(close):
        return hits

    # 1 MCDX
    if len(dd) >= N + SMA + 5:
        pc = fmcdx(c * 0.96, h, l)
        lc = 100.0 - fmcdx(c * 1.04, h, l)
        sp = float(pc.rolling(SMA, min_periods=SMA).mean().iloc[-1])
        sl = float(lc.rolling(SMA, min_periods=SMA).mean().iloc[-1])
        if not pd.isna(sp) and not pd.isna(sl) and sp < sl:
            hits["01_mcdx"] = True

    # 2 EMA200
    if len(dd) >= 200:
        e200 = float(c.ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1])
        if not pd.isna(e200) and close < e200:
            hits["02_ema200"] = True

    # 3 EMA50
    if len(dd) >= 50:
        e50 = float(c.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1])
        if not pd.isna(e50) and close < e50:
            hits["03_ema50"] = True

    # 5 daily TD S2
    if len(dd) >= 5 and td_match(pd.DataFrame({"Close": c}), DEFAULT_TD_D):
        hits["05_td_daily"] = True

    # weekly gates 6–8
    w = to_weekly(dd)
    if not forming:
        w = drop_forming_weekly(w)
    if w is not None and not w.empty and len(w) >= 5:
        if td_match(w, DEFAULT_TD_W):
            hits["06_td_weekly"] = True
        if zhuli_bear_gt_bull(w):
            hits["07_zhuli"] = True
        if kdj_yellow(w):
            hits["08_kdj_weekly"] = True

    # monthly 9+11 (completed) + 10 (forming included)
    m_all = to_monthly(dd)
    m_done = drop_forming_monthly(m_all) if not forming else m_all
    if m_done is not None and not m_done.empty and kdj_yellow(m_done):
        hits["09_kdj_monthly"] = True
    m_sar = m_all if (m_all is not None and len(m_all) >= 3) else m_done
    if m_sar is not None and not m_sar.empty and monthly_sar_above_close(m_sar):
        hits["10_sar_monthly"] = True
    if m_done is not None and not m_done.empty and zhuli_bear_gt_bull(m_done):
        hits["11_zhuli_monthly"] = True

    return hits


def write_local(gate_hits: dict[str, list[str]], out_dir: Path, asof: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    combine_rows: list[list[str]] = []
    for key in GATE_ORDER:
        syms = sorted(set(gate_hits.get(key, [])))
        label = GATE_LABELS[key]
        path = out_dir / f"exclude_{key}.txt"
        path.write_text("\n".join(syms) + ("\n" if syms else ""), encoding="utf-8")
        csv_path = out_dir / f"exclude_{key}.csv"
        csv_path.write_text(
            "symbol,gate\n" + "".join(f"{s},{label}\n" for s in syms),
            encoding="utf-8",
        )
        for s in syms:
            combine_rows.append([s, label])
        print(f"  {key}: {len(syms)} → {path.name}", flush=True)

    combine_rows.sort(key=lambda r: (r[0], r[1]))
    combine = out_dir / "us_exclude_screener_combine.csv"
    with combine.open("w", encoding="utf-8") as f:
        f.write("symbol,gate\n")
        for s, g in combine_rows:
            f.write(f"{s},{g}\n")
    meta = out_dir / "run_meta.txt"
    meta.write_text(
        f"asof_et={asof}\n"
        f"gates={len(GATE_ORDER)}\n"
        f"combine_rows={len(combine_rows)}\n"
        f"unique_symbols={len({r[0] for r in combine_rows})}\n",
        encoding="utf-8",
    )
    print(f"combine rows={len(combine_rows)} → {combine}", flush=True)
    return combine


def maybe_write_sheets(gate_hits: dict[str, list[str]], sheet_ids_path: Path) -> None:
    """Write to Google Sheets if GOOGLE_SERVICE_ACCOUNT_JSON is set."""
    import os

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        print("Sheets: skip (no GOOGLE_SERVICE_ACCOUNT_JSON)", flush=True)
        return
    try:
        from sheets_writer import write_all_gates
    except ImportError:
        print("Sheets: sheets_writer import failed", flush=True)
        return
    cfg = json.loads(sheet_ids_path.read_text(encoding="utf-8"))
    write_all_gates(
        sa_json, cfg, gate_hits, GATE_LABELS, GATE_ORDER,
        dup_priority=DUP_GATE_PRIORITY,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="US exclude screener — 11 separate gates")
    ap.add_argument("--symbols", default="", help="Comma/space symbols; empty=full universe")
    ap.add_argument("--forming", action="store_true", help="Include forming daily bar")
    ap.add_argument("--out-dir", default="out", help="Local output directory")
    ap.add_argument("--sheet-ids", default="sheet_ids.json", help="Sheet ID config")
    ap.add_argument("--skip-sheets", action="store_true", help="Never write Google Sheets")
    ap.add_argument("--gates", default="", help="Comma gate keys to run (default=all)")
    args = ap.parse_args()

    if args.symbols.strip():
        syms = parse_syms(args.symbols)
    else:
        print("universe...", flush=True)
        syms = universe()
    print(f"universe={len(syms)}", flush=True)

    want = set(GATE_ORDER)
    if args.gates.strip():
        want = {g.strip() for g in args.gates.replace(",", " ").split() if g.strip()}
        unknown = want - set(GATE_ORDER)
        if unknown:
            print("unknown gates:", unknown, flush=True)
            return 2

    data = download(syms)
    print(f"data={len(data)}", flush=True)

    gate_hits: dict[str, list[str]] = {k: [] for k in GATE_ORDER}
    for i, (s, d) in enumerate(data.items()):
        hits = eval_gates(d, forming=args.forming)
        for k, v in hits.items():
            if v and k in want:
                gate_hits[k].append(s)
        if i % 500 == 0:
            print(f"scan {i}/{len(data)}", flush=True)

    if "04_mcap" in want:
        print(f"mcap check {len(data)}...", flush=True)
        with ThreadPoolExecutor(max_workers=MCAP_WORKERS) as ex:
            futs = {ex.submit(fetch_mcap, s): s for s in data}
            done = 0
            for fut in as_completed(futs):
                s = futs[fut]
                done += 1
                if done % 200 == 0:
                    print(f"mcap {done}/{len(data)}", flush=True)
                try:
                    cap = fut.result()
                except Exception:
                    cap = None
                if cap is not None and cap < MCAP_MAX:
                    gate_hits["04_mcap"].append(s)

    # Drop gates not requested
    for k in list(gate_hits):
        if k not in want:
            gate_hits[k] = []

    asof = ny_now().strftime("%Y-%m-%d %H:%M ET")
    out_dir = Path(args.out_dir)
    write_local(gate_hits, out_dir, asof)

    if not args.skip_sheets:
        maybe_write_sheets(gate_hits, Path(args.sheet_ids))

    for k in GATE_ORDER:
        print(f"COUNT {k}={len(set(gate_hits[k]))} ({GATE_LABELS[k]})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
