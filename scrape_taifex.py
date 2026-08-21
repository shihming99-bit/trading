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

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# 台指期三大法人未平倉（口數）
URL_OI = "https://stock.wearn.com/taifexphoto.asp"       # k 省略＝台指期
CSV_OI = os.path.join(DATA_DIR, "taifex_oi.csv")

# 三大法人現貨買賣超（億元）
URL_FUND = "https://stock.wearn.com/fundthree.asp"
CSV_FUND = os.path.join(DATA_DIR, "fund_netbuy.csv")

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


def fetch_html(url: str) -> str:
    """帶重試的抓取；逾時或連線錯誤會等待後重試，最後才拋出。"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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
        except requests.exceptions.HTTPError:
            raise  # HTTP 4xx/5xx 不重試
    raise RuntimeError(f"連線 {url} 連續 {MAX_RETRIES} 次失敗：{last_err}")


def fetch_tables(url: str) -> list[pd.DataFrame]:
    return pd.read_html(io.StringIO(fetch_html(url)))


def header_row_index(df: pd.DataFrame) -> int:
    """回傳含『日期』的標頭列 index。"""
    for i in range(min(6, len(df))):
        if df.iloc[i].str.contains("日期").any():
            return i
    raise RuntimeError("找不到標頭列（『日期』）。")


# ---------- 台指期未平倉 ----------
def pick_oi_table(tables):
    for t in tables:
        joined = " ".join(t.astype(str).head(3).values.ravel().tolist())
        if "外資" in joined and "收盤" in joined:
            return t
    raise RuntimeError("找不到台指期未平倉表，wearn 版面可能已改動。")


def normalize_oi(t: pd.DataFrame) -> pd.DataFrame:
    df = t.astype(str)
    hi = header_row_index(df)
    body = df.iloc[hi + 1:].copy()
    body = body[body.iloc[:, 0].str.match(r"^\d{3}/\d{2}/\d{2}$", na=False)]
    if body.empty:
        raise RuntimeError("未平倉表解析後沒有有效資料列。")
    body = body.iloc[:, :9]
    body.columns = COLS
    for c in COLS[1:]:
        body[c] = (body[c].str.replace(",", "", regex=False)
                          .astype(float).round().astype("Int64"))
    body["date"] = body["date_roc"].map(roc_to_iso)
    return body[["date"] + COLS].reset_index(drop=True)


# ---------- 三大法人現貨買賣超（億元）----------
FUND_COLS = ["date_roc", "trust", "dealer", "foreign"]   # 對應 wearn 欄序：投信/自營商/外資


def _num(x):
    x = str(x).replace(",", "").replace(" ", "")
    if x in ("", "nan"):
        return None
    return float(x)


def pick_fund_table(tables):
    """含日期、投信、外資三者的那張明細表（非上方統計小表）。"""
    for t in tables:
        s = t.astype(str)
        joined = " ".join(s.head(3).values.ravel().tolist())
        if "日期" in joined and "投信" in joined and "外資" in joined:
            return t
    raise RuntimeError("找不到三大法人買賣超表，wearn 版面可能已改動。")


def normalize_fund(t: pd.DataFrame) -> pd.DataFrame:
    df = t.astype(str)
    hi = header_row_index(df)
    header = df.iloc[hi].tolist()

    def col_for(key):                       # 依欄名定位，不靠固定位置
        for j, name in enumerate(header):
            if key in name:
                return j
        raise RuntimeError(f"買賣超表缺少欄位：{key}")

    c_trust, c_dealer, c_foreign = col_for("投信"), col_for("自營商"), col_for("外資")
    body = df.iloc[hi + 1:]
    body = body[body.iloc[:, 0].str.match(r"^\d{3}/\d{2}/\d{2}$", na=False)]
    if body.empty:
        raise RuntimeError("買賣超表解析後沒有有效資料列。")

    rows = []
    for _, r in body.iterrows():
        rows.append([r.iloc[0], _num(r.iloc[c_trust]),
                     _num(r.iloc[c_dealer]), _num(r.iloc[c_foreign])])
    out = pd.DataFrame(rows, columns=FUND_COLS)
    out["date"] = out["date_roc"].map(roc_to_iso)
    return out[["date"] + FUND_COLS].reset_index(drop=True)


# ---------- 通用合併 ----------
def merge_csv(new: pd.DataFrame, path: str) -> tuple[int, int]:
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={"date_roc": str})
        before = len(old)
        combined = pd.concat([old, new], ignore_index=True)
    else:
        before = 0
        combined = new.copy()
    combined = (combined.drop_duplicates(subset="date", keep="last")
                        .sort_values("date").reset_index(drop=True))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    combined.to_csv(path, index=False)
    return len(combined), max(len(combined) - before, 0)


def run_one(label, url, mock_env, picker, normalizer, csv_path):
    src = os.environ.get(mock_env)
    tables = (pd.read_html(io.StringIO(open(src, encoding="utf-8").read()))
              if src else fetch_tables(url))
    new = normalizer(picker(tables))
    total, added = merge_csv(new, csv_path)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {label}：解析 {len(new)} 列，"
          f"最新 {new['date'].max()}；CSV 現有 {total} 列，本次淨增 {added} 列。")


def main():
    errors = []
    # 兩份資料彼此獨立：其中一份失敗不影響另一份
    for args in [
        ("未平倉", URL_OI, "MOCK_HTML", pick_oi_table, normalize_oi, CSV_OI),
        ("買賣超", URL_FUND, "MOCK_FUND", pick_fund_table, normalize_fund, CSV_FUND),
    ]:
        try:
            run_one(*args)
        except Exception as e:
            errors.append(f"{args[0]}：{e}")
            print(f"  ⚠ {args[0]} 抓取失敗：{e}", file=sys.stderr)
    if errors:
        raise RuntimeError("；".join(errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)
