#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions headless: 唔考慮篩選（預設同 mcdx_exclude app）

OR exclude legs include: MCDX, close<EMA200/50, mcap<100M, daily TD S2,
weekly TD S1/S2, weekly 主力能量 bear>bull, weekly KDJ yellow (J crossunder 50),
monthly KDJ yellow (J crossunder 50).
"""
from __future__ import annotations

import io
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

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
    if w.empty:
        return w
    now = ny_now()
    last = pd.Timestamp(w.index[-1])
    # drop incomplete week (before Friday close-ish)
    if last.date() >= (now.date() - timedelta(days=now.weekday())) and now.weekday() < 4:
        return w.iloc[:-1] if len(w) > 1 else w
    return w


def to_weekly(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x.index = pd.to_datetime(x.index)
    o = x["Open"].resample("W-FRI").first()
    h = x["High"].resample("W-FRI").max()
    l = x["Low"].resample("W-FRI").min()
    c = x["Close"].resample("W-FRI").last()
    v = x["Volume"].resample("W-FRI").sum() if "Volume" in x.columns else None
    w = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c})
    if v is not None:
        w["Volume"] = v
    return w.dropna(subset=["Close", "High", "Low"])


def to_monthly(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x.index = pd.to_datetime(x.index)
    o = x["Open"].resample("ME").first()
    h = x["High"].resample("ME").max()
    l = x["Low"].resample("ME").min()
    c = x["Close"].resample("ME").last()
    v = x["Volume"].resample("ME").sum() if "Volume" in x.columns else None
    m = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c})
    if v is not None:
        m["Volume"] = v
    return m.dropna(subset=["Close", "High", "Low"])


def drop_forming_monthly(m: pd.DataFrame) -> pd.DataFrame:
    """Drop incomplete current month before month-end / before NY close-ish."""
    if m.empty:
        return m
    now = ny_now()
    last = pd.Timestamp(m.index[-1])
    # Still in the calendar month of the last bar, and not yet past that month's
    # last calendar day close: treat as forming until next month starts (or after
    # last day 16:15 ET). Month-end bar label is month-end date.
    month_end = (last + pd.offsets.MonthEnd(0)).date()
    if now.date() < month_end:
        return m.iloc[:-1] if len(m) > 1 else m
    if now.date() == month_end and (now.hour, now.minute) < (16, 15):
        return m.iloc[:-1] if len(m) > 1 else m
    return m


def kdj_j(df: pd.DataFrame) -> pd.Series:
    """Tongdaxin KDJ (9,3,3) J line using CN recursive SMA."""
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
    """True if last completed bar is yellow: J crossunder 50 (prev>=50, curr<50)."""
    if df is None or len(df) < 15:
        return False
    j = kdj_j(df)
    if len(j) < 2:
        return False
    prev, curr = j.iloc[-2], j.iloc[-1]
    if pd.isna(prev) or pd.isna(curr):
        return False
    return float(prev) >= 50.0 and float(curr) < 50.0


def sma_cn(src: pd.Series, length: int) -> pd.Series:
    out = []
    y = None
    for v in src.astype(float):
        if pd.isna(v):
            out.append(float("nan"))
            continue
        y = v if y is None or pd.isna(y) else (v + (length - 1) * y) / length
        out.append(y)
    return pd.Series(out, index=src.index)


def zhuli_bear_gt_bull(w: pd.DataFrame) -> bool:
    if len(w) < 40:
        return False
    h, l, c = w["High"].astype(float), w["Low"].astype(float), w["Close"].astype(float)
    hh36, ll36 = h.rolling(36).max(), l.rolling(36).min()
    rng36 = hh36 - ll36
    var1 = ((hh36 - c) / rng36.replace(0, pd.NA) * 100).fillna(0)
    bear = sma_cn(var1, 3)
    hh9, ll9 = h.rolling(9).max(), l.rolling(9).min()
    rng9 = hh9 - ll9
    var2 = ((c - ll9) / rng9.replace(0, pd.NA) * 100).fillna(0)
    bull = sma_cn(var2, 5) - 8
    b, u = bear.iloc[-1], bull.iloc[-1]
    return not (pd.isna(b) or pd.isna(u)) and float(b) > float(u)


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
    return (
        pd.to_numeric(d[cols["close"]], errors="coerce"),
        pd.to_numeric(d[cols["high"]], errors="coerce"),
        pd.to_numeric(d[cols["low"]], errors="coerce"),
    )


def td_setup_counts(close: pd.Series):
    c = close.astype(float)
    buy = sell = 0
    bs, ss = [], []
    for i in range(len(c)):
        if i < 4:
            bs.append(0)
            ss.append(0)
            continue
        if c.iloc[i] > c.iloc[i - 4]:
            buy = buy + 1 if buy < 9 else 1
            sell = 0
        elif c.iloc[i] < c.iloc[i - 4]:
            sell = sell + 1 if sell < 9 else 1
            buy = 0
        else:
            buy = sell = 0
        bs.append(buy)
        ss.append(sell)
    return pd.Series(bs, index=c.index), pd.Series(ss, index=c.index)


def td_match(df: pd.DataFrame, codes: set[str]) -> bool:
    if not codes or len(df) < 10:
        return False
    c = df["Close"].astype(float)
    b, s = td_setup_counts(c)
    bv, sv = int(b.iloc[-1]), int(s.iloc[-1])
    for code in codes:
        if code.startswith("B") and bv == int(code[1:]):
            return True
        if code.startswith("S") and sv == int(code[1:]):
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
                    bt, period="5y", interval="1d", group_by="ticker",
                    auto_adjust=False, threads=True, progress=False,
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
                d = d.dropna(subset=["Close", "High", "Low"])
                if not d.empty:
                    out[s] = d
            except Exception:
                pass
        time.sleep(0.15)
    return out


def exclude_hit(d: pd.DataFrame) -> bool:
    d = drop_forming(d)
    if d.empty or len(d) < 50:
        return False
    try:
        c, h, l = _ohlc(d)
    except Exception:
        return False
    close = float(c.iloc[-1])

    # 1 MCDX
    if len(d) >= N + SMA + 5:
        pc = fmcdx(c * 0.96, h, l)
        lc = 100.0 - fmcdx(c * 1.04, h, l)
        sp = float(pc.rolling(SMA, min_periods=SMA).mean().iloc[-1])
        sl = float(lc.rolling(SMA, min_periods=SMA).mean().iloc[-1])
        if not pd.isna(sp) and not pd.isna(sl) and sp < sl:
            return True

    # 2 EMA200
    if len(d) >= 200:
        e200 = float(c.ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1])
        if not pd.isna(e200) and close < e200:
            return True

    # 3 EMA50
    e50 = float(c.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1])
    if not pd.isna(e50) and close < e50:
        return True

    # 5 daily TD S2
    if td_match(pd.DataFrame({"Close": c}), DEFAULT_TD_D):
        return True

    ohlc_w = pd.DataFrame({"Open": d["Open"], "High": h, "Low": l, "Close": c, "Volume": d.get("Volume", c * 0)})
    w = drop_forming_weekly(to_weekly(ohlc_w))
    if len(w) >= 10:
        # 6 weekly TD S1/S2
        if td_match(w, DEFAULT_TD_W):
            return True
        # 7 zhuli
        if zhuli_bear_gt_bull(w):
            return True
        # 8 weekly KDJ yellow (J crossunder 50)
        if kdj_yellow(w):
            return True

    # 9 monthly KDJ yellow (J crossunder 50)
    m = drop_forming_monthly(to_monthly(ohlc_w))
    if kdj_yellow(m):
        return True
    return False


def main() -> int:
    print("universe...", flush=True)
    syms = universe()
    print(f"universe={len(syms)}", flush=True)
    data = download(syms)
    print(f"data={len(data)}", flush=True)

    hits: list[str] = []
    need_mcap: list[str] = []
    for i, (s, d) in enumerate(data.items()):
        if exclude_hit(d):
            hits.append(s)
        else:
            need_mcap.append(s)
        if i % 500 == 0:
            print(f"scan {i}/{len(data)} hits={len(hits)}", flush=True)

    # 4 mcap < 100M only for not-yet-hit
    print(f"mcap check {len(need_mcap)}...", flush=True)
    with ThreadPoolExecutor(max_workers=MCAP_WORKERS) as ex:
        futs = {ex.submit(fetch_mcap, s): s for s in need_mcap}
        done = 0
        for fut in as_completed(futs):
            s = futs[fut]
            done += 1
            if done % 200 == 0:
                print(f"mcap {done}/{len(need_mcap)}", flush=True)
            try:
                cap = fut.result()
            except Exception:
                cap = None
            if cap is not None and cap < MCAP_MAX:
                hits.append(s)

    hits = sorted(set(hits))
    asof = ny_now().strftime("%Y-%m-%d %H:%M ET")
    out = Path("exclude_symbols.txt")
    out.write_text("\n".join(hits) + ("\n" if hits else ""), encoding="utf-8")
    print("===== EXCLUDE SYMBOLS BEGIN =====")
    print("\n".join(hits))
    print("===== EXCLUDE SYMBOLS END =====")
    print(f"count={len(hits)} asof≈{asof} file={out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
