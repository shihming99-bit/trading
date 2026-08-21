# 台指期三大法人未平倉 · 每日自動抓取

每個交易日盤後自動從聚財網 `stock.wearn.com` 抓取台指期三大法人（外資／投信／自營商）
未平倉淨額，累積存進 `data/taifex_oi.csv`。跑在 GitHub Actions 上，不需要自己開電腦。

> 資料原始來源為台灣期交所公告，經聚財網整理。僅供參考，不構成投資建議，投資請自負風險。

## 檔案

| 檔案 | 用途 |
| --- | --- |
| `scrape_taifex.py` | 抓取、解析、合併去重、寫回 CSV |
| `data/taifex_oi.csv` | 累積資料（已含 6/10–8/20 種子資料 50 筆） |
| `.github/workflows/daily.yml` | 每日排程 |

CSV 欄位：`date`(西元) · `date_roc`(民國) · `top5` · `top10` · `top5_sp` · `top10_sp` ·
`foreign`(外資) · `trust`(投信) · `dealer`(自營商) · `close`(收盤)。

## 一次性設定（約 5 分鐘）

1. 在 GitHub 建一個新 repo（private 即可），把這個資料夾的內容整包上傳／push 上去。
2. 進 repo 的 **Settings → Actions → General**，
   捲到 **Workflow permissions**，選 **Read and write permissions**，存檔。
   （workflow 需要這個權限才能把更新後的 CSV 推回。）
3. 進 **Actions** 分頁，若看到提示就按 **Enable workflows**。
4. 點左側 **台指期未平倉每日更新 → Run workflow** 手動跑一次，確認會綠燈通過、
   且 `data/taifex_oi.csv` 有被更新 commit。之後就會每個工作日自動跑。

## 排程時間

`daily.yml` 設定為 UTC 週一至週五 14:30（＝台灣時間 22:30），對應期交所盤後公告後。
要改時間就改 `cron` 那行（注意 cron 用 UTC，台灣時間需 −8 小時）。
GitHub 排程在尖峰時段可能延遲數十分鐘屬正常。

## 手動補跑

漏抓或想立即更新，到 Actions 頁按 **Run workflow** 即可。
腳本會自動去重，重複日期只會覆蓋不會產生重複列。

## 本機測試（可選）

```bash
pip install requests pandas lxml beautifulsoup4
python scrape_taifex.py          # 直接抓線上
MOCK_HTML=some.html python scrape_taifex.py   # 用本地 HTML 測解析
```

## 版面異動時

若 wearn 改版導致解析失敗，Actions 會紅燈，腳本會印出錯誤訊息
（找不到表 / 找不到標頭 / 無有效資料列）。屆時需依新版面調整
`pick_oi_table` 或 `normalize` 的判斷條件。
