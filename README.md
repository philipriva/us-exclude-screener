# US Exclude Screener（唔考慮 — 11 閘分開跑）

收市後自動跑 `us_exclude_screener_app.pyw` 嘅 **11 條 OR 閘**（各自獨立），寫入 Google Drive **`GROK/github`** 入面 **11 個 `exclude_*` spreadsheet**，再更新合併表 **`us_exclude_screener`**（喺 **`processing`**：tab `combine`：A=symbol，B=閘名；tab `Duplicate`：每隻股一行，B 欄閘名按重視排名串）。

## 十一條閘（預設同桌面 app）

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
| 11 | `exclude_11_zhuli_monthly` | 月線主力能量 | 做空線 &gt; 做多線（同閘7）；**只用已收市月** |

- **宇宙**：Nasdaq Trader 全美（含 ETF／Fund）；剔測試／`$`／`^`
- **價**：Yahoo `auto_adjust=True`
- **未收市日／週 K**：閘 1–8 預設丟掉（美東 16:15 前）；閘 9／11 用已收市月；閘 10 含未收市月
- **未做**：月線綠帶（赚钱轨道）

## Drive

- 合併表 **`us_exclude_screener`** → folder **processing**  
  https://drive.google.com/drive/folders/1N41jCmxOvOzNQXRtE6DwYojyU5kskqZM
- 閘檔 **`exclude_01`～`exclude_11`** → folder **GROK/github**  
  https://drive.google.com/drive/folders/1bysfp2HI3muZDbIYm_zI4gjhp7ir5g3A  

IDs 見 `sheet_ids.json`（Actions／本地有 SA 會用 **file id** 覆寫內容，搬 folder 唔影響）。

## GitHub Actions

- **排程**：`cron: 0 6 * * 2-6`（UTC，Tue–Sat）
  - **對應美股**：Mon–Fri 常規收市（16:00 ET）之後約 **9–10 小時**
  - **美東**：約 **02:00 EDT**／**01:00 EST**（下一個交易日 09:30 ET 開市前仍有充足時間）
  - **香港（HKT）**：約 **14:00**（Tue–Sat）
  - **點解唔用更早（例如 01:00 UTC）**：收市後 4–5 小時 Yahoo 日線／市值有時仍未穩；凌晨段通常較齊，跑完亦一定喺下個開市前
- **手動**：Actions → *Daily Exclude Screener* → Run workflow（`workflow_dispatch`）
- **Artifact**：`exclude_gates`（`out/` 入面每閘 `.txt`／`.csv` + combine），保留 14 日

### 必設 Secret（Sheets 寫入）

| Secret | 內容 |
|--------|------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud **service account** 嘅整份 JSON（一字不漏） |

設定步驟（只需一次）：

1. Google Cloud Console → 開 project → 啟用 **Google Sheets API** + **Google Drive API**
2. 建 Service Account → 下載 JSON key
3. 把 SA 電郵（`...@....iam.gserviceaccount.com`）加做 **`processing`**（合併表）同 **`GROK/github`**（閘檔）嘅 **Editor**（或分享晒相關 spreadsheet）
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


## Overnight first populate (2026-10-19 HKT)

Full Nasdaq Trader universe was scanned locally; gate sheets live in `GROK/github`, combine sheet in `processing` (see `sheet_ids.json`).  
**市值 &lt; 100M** returned **0** hits that night because Yahoo Finance returned crumb/401 during the mcap pass — re-run the workflow after adding the SA secret (or local with working Yahoo) to refresh gate 4.
