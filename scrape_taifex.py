#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取聚財網 stock.wearn.com 台指期三大法人未平倉淨額，合併進 data/taifex_oi.csv。
資料原始來源為台灣期交所公告，經聚財網整理。僅供參考，不構成投資建議。
"""

import io
import os
import sys
import time
import datetime as dt

import requests
import pandas as pd

URL = "https://stock.wearn.com/taifexphoto.asp"          # k 省略＝台指期
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "taifex_oi.csv")

# 連線設定：GitHub Actions 從國外連台灣站點偶有逾時，故加大 timeout 並重試
TIMEOUT = 45          # 秒
MAX_RETRIES = 5       # 逾時／連線失敗時的重試次數
BACKOFF = 8           # 每次重試前的等待秒數（遞增）

# 目標欄位（對應 wearn 表格）
COLS = ["date_roc", "top5", "top10", "top5_sp", "top10_sp",
        "foreign", "trust", "dealer", "close"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://stock.wearn.com/",
}


def roc_to_iso(roc: str) -> str:
    """民國 115/08/20 -> 2026-08-20"""
    y, m, d = roc.split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def fetch_html() -> str:
    """帶重試的抓取；逾時或連線錯誤會等待後重試，最後才拋出。"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last_err = e
            wait = BACKOFF * attempt
            print(f"  第 {attempt}/{MAX_RETRIES} 次連線失敗（{type(e).__name__}），"
                  f"{wait}s 後重試…", file=sys.stderr)
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            # HTTP 4xx/5xx 不重試，直接拋出
            raise
    raise RuntimeError(f"連線 wearn 連續 {MAX_RETRIES} 次失敗：{last_err}")


def fetch_tables() -> list[pd.DataFrame]:
    # pandas.read_html 會回傳頁面上所有 <table>
    return pd.read_html(io.StringIO(fetch_html()))


def pick_oi_table(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """找出含『外資』與『收盤』且日期為民國格式的那張表。"""
    for t in tables:
        flat = t.astype(str)
        joined = " ".join(flat.head(3).values.ravel().tolist())
        if "外資" in joined and "收盤" in joined:
            return t
    raise RuntimeError("找不到台指期未平倉表，wearn 版面可能已改動。")


def normalize(t: pd.DataFrame) -> pd.DataFrame:
    df = t.astype(str)
    # 找出標頭列（含『日期』），其下才是資料
    header_idx = None
    for i in range(min(5, len(df))):
        if df.iloc[i].str.contains("日期").any():
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("找不到標頭列（『日期』）。")

    body = df.iloc[header_idx + 1:].copy()
    # 只留下第一欄為民國日期（\d{3}/\d{2}/\d{2}）的列
    date_re = r"^\d{3}/\d{2}/\d{2}$"
    body = body[body.iloc[:, 0].str.match(date_re, na=False)]
    if body.empty:
        raise RuntimeError("解析後沒有有效資料列。")

    body = body.iloc[:, :9]               # 取前 9 欄
    body.columns = COLS
    for c in COLS[1:]:                     # 數字欄去逗號轉 int
        body[c] = (body[c].str.replace(",", "", regex=False)
                          .astype(float).round().astype("Int64"))
    body["date"] = body["date_roc"].map(roc_to_iso)
    return body[["date"] + COLS].reset_index(drop=True)


def merge_csv(new: pd.DataFrame) -> tuple[int, int]:
    if os.path.exists(CSV_PATH):
        old = pd.read_csv(CSV_PATH, dtype={"date_roc": str})
        combined = pd.concat([old, new], ignore_index=True)
    else:
        combined = new.copy()

    before = 0 if not os.path.exists(CSV_PATH) else len(old)
    # 依 date 去重，保留最後出現者（新抓的覆蓋舊的），再依日期排序
    combined = (combined.drop_duplicates(subset="date", keep="last")
                        .sort_values("date")
                        .reset_index(drop=True))
    added = len(combined) - before
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    combined.to_csv(CSV_PATH, index=False)
    return len(combined), max(added, 0)


def main():
    src = os.environ.get("MOCK_HTML")     # 測試用：讀本地 HTML
    if src:
        with open(src, encoding="utf-8") as f:
            tables = pd.read_html(io.StringIO(f.read()))
    else:
        tables = fetch_tables()

    oi = pick_oi_table(tables)
    new = normalize(oi)
    total, added = merge_csv(new)
    latest = new["date"].max()
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] 解析 {len(new)} 列，"
          f"最新 {latest}；CSV 現有 {total} 列，本次淨增 {added} 列。")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)
