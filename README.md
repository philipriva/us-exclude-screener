# US Exclude Screener（唔考慮 — 10 閘分開跑）

收市後自動跑 `us_exclude_screener_app.pyw` 嘅 **10 條 OR 閘**（各自獨立），寫入 Google Drive **`GROK/github`** 入面 **10 個 spreadsheet**，再更新合併表 **`us_exclude_screener`**（tab `combine`：A=symbol，B=閘名）。

## 十條閘（預設同桌面 app）

| # | Sheet | 閘標籤（combine 欄 B） | 規則 |
|---|-------|------------------------|------|
| 1 | `exclude_01_mcdx` | 日線 MCDX 籌碼 | SMA10(PC)&lt;SMA10(LC)；PC=fmcdx(C×0.96)，LC=100−fmcdx(C×1.04)，N=100 |
| 2 | `exclude_02_ema200` | 日線 close &lt; EMA200 | |
| 3 | `exclude_03_ema50` | 日線 close &lt; EMA50 | |
| 4 | `exclude_04_mcap` | 市值 &lt; 100M | 查唔到 mcap → 跳過，唔當 hit |
| 5 | `exclude_05_td_daily` | 日線 TD Sequential | 預設只 S2（S＝close&lt;close[4]） |
| 6 | `exclude_06_td_weekly` | 週線 TD Sequential | 預設 S1+S2（週線由日線合成） |
| 7 | `exclude_07_zhuli` | 週線主力能量 | 做空線 &gt; 做多線 |
| 8 | `exclude_08_kdj_weekly` | 週線 KDJ 黃柱 | KDJ(9,3,3) J 由 ≥50 跌穿 50 |
| 9 | `exclude_09_kdj_monthly` | 月線 KDJ 黃柱 | 同上；**只用已收市月** |
| 10 | `exclude_10_sar_monthly` | 月線 SAR 喺價上 | TV `ta.sar(0.02,0.02,0.2)`；`close&lt;SAR`；**含未收市月** |

- **宇宙**：Nasdaq Trader 全美（含 ETF／Fund）；剔測試／`$`／`^`
- **價**：Yahoo `auto_adjust=True`
- **未收市日／週 K**：閘 1–8 預設丟掉（美東 16:15 前）
- **未做**：月線綠帶（赚钱轨道）

## Drive

Folder：**GROK → github**  
IDs 見 `sheet_ids.json`（已預建空表；Actions／本地有 SA 會覆寫內容）。

## GitHub Actions

- **排程**：`cron: 0 1 * * 2-6`（UTC）≈ 美東 Mon–Fri 收市後 4–5 小時  
- **手動**：Actions → *Daily Exclude Screener* → Run workflow  
- **Artifact**：`exclude_gates`（`out/` 入面每閘 `.txt`／`.csv` + combine），保留 14 日

### 必設 Secret（Sheets 寫入）

| Secret | 內容 |
|--------|------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud **service account** 嘅整份 JSON（一字不漏） |

設定步驟（只需一次）：

1. Google Cloud Console → 開 project → 啟用 **Google Sheets API** + **Google Drive API**
2. 建 Service Account → 下載 JSON key
3. 把 SA 電郵（`...@....iam.gserviceaccount.com`）加做 Drive 資料夾 **`GROK/github`** 嘅 **Editor**（或分享晒入面 11 個 spreadsheet）
4. Repo → Settings → Secrets → Actions → New：名 `GOOGLE_SERVICE_ACCOUNT_JSON`，值＝整份 JSON

冇呢個 secret 時，workflow **仍然會跑完**並上傳 artifact，只係跳過 Sheets 寫入。

## 本地

```bash
pip install -r requirements.txt
# smoke
python run_exclude.py --symbols AAPL,TSLA,ABT,AMT,ADBE --skip-sheets --out-dir out
# full + Sheets
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/sa.json)"
python run_exclude.py --out-dir out
```

## 公式來源

`us_exclude_screener_app.pyw`（桌面「開啟唔考慮篩選」）。
