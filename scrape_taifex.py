#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取聚財網 stock.wearn.com 台指期三大法人未平倉淨額，合併進 data/taifex_oi.csv。
資料原始來源為台灣期交所公告，經聚財網整理。僅供參考，不構成投資建議。
"""

import io
import os
import re as _re
import sys
import time
import datetime as dt

import requests
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# 台指期三大法人未平倉（口數）
URL_OI = "https://stock.wearn.com/taifexphoto.asp"       # k 省略＝台指期
CSV_OI = os.path.join(DATA_DIR, "taifex_oi.csv")

# 三大法人現貨買賣超（億元）— 改用證交所官方 BFI82U（與官網統計表一致）
# 說明：官方一天產製兩次（14:50 不含綜合帳戶/鉅額、19:40 含），排程設晚一點抓到含鉅額的確定版
URL_FUND = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&type=day"
CSV_FUND = os.path.join(DATA_DIR, "fund_netbuy.csv")

# 全市場收盤價（每日覆蓋，不累積歷史）— 官方即時端點（盤後約16:00更新）
# 證交所 MI_INDEX：多表 JSON，個股收盤在「每日收盤行情」表；type=ALLBUT0999 為全部（不含權證等）
URL_PRICE_TWSE = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999"
# 大盤整體融資融券餘額（信用交易統計）— 證交所官方 MI_MARGN
URL_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=MS"
CSV_MARGIN = os.path.join(DATA_DIR, "margin.csv")
# 櫃買中心（上櫃）每日收盤行情
URL_PRICE_TPEX = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?response=json"
CSV_PRICE = os.path.join(DATA_DIR, "prices.csv")

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
        joined = " ".join(str(v) for v in t.astype(str).head(3).values.ravel())
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


# ---------- 三大法人現貨買賣超（億元）— 證交所官方 BFI82U ----------
FUND_COLS = ["date_roc", "trust", "dealer", "foreign"]   # 投信/自營商(合計)/外資


def _yi(n):
    """元 → 億元，四捨五入到小數兩位。"""
    return round(n / 1e8, 2)


def run_fund():
    """抓官方 BFI82U，合併進 fund_netbuy.csv。自營商=自行買賣+避險；外資=外資及陸資(不含外資自營商)。"""
    mock = os.environ.get("MOCK_FUND_JSON")
    payload = (_json.load(open(mock, encoding="utf-8")) if mock
               else _fetch_json(URL_FUND))
    tables = payload.get("tables") or ([payload] if "data" in payload else [])
    # BFI82U 只有一張表；相容處理
    t = None
    for cand in tables:
        title = str(cand.get("title", ""))
        fields = " ".join(str(x) for x in cand.get("fields", []))
        if "買賣差額" in fields or "買賣差額" in title or "三大法人" in title:
            t = cand
            break
    if t is None and tables:
        t = tables[0]
    if t is None:
        raise RuntimeError("BFI82U 找不到資料表")

    iso = _roc_title_to_iso(t.get("title", ""))
    roc = ""
    m = _re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", str(t.get("title", "")))
    if m:
        roc = f"{int(m.group(1)):03d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # 各類別買賣差額（第 4 欄，index 3），單位:元
    diff = {}
    for r in t.get("data", []):
        name = str(r[0]).strip()
        try:
            diff[name] = float(str(r[3]).replace(",", "").strip())
        except (ValueError, IndexError):
            continue

    def get(*keys):
        for k in keys:
            for name, v in diff.items():
                if k in name:
                    return v
        return 0.0

    trust = _yi(get("投信"))
    dealer = _yi(get("自營商(自行買賣)", "自行買賣") + get("自營商(避險)", "避險"))
    foreign = _yi(get("外資及陸資"))

    new = pd.DataFrame([[roc, trust, dealer, foreign]], columns=FUND_COLS)
    new["date"] = iso or dt.date.today().isoformat()
    new = new[["date"] + FUND_COLS]

    total, added = merge_csv(new, CSV_FUND)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] 買賣超：交易日 {new['date'].iloc[0]}"
          f"（投信 {trust}、自營商 {dealer}、外資 {foreign} 億）；"
          f"CSV 現有 {total} 列，本次淨增 {added} 列。")


# ---------- 大盤融資融券餘額 — 證交所官方 MI_MARGN ----------
MARGIN_COLS = ["date_roc", "margin_bal", "short_bal"]  # 融資餘額(億)、融券餘額(張)


def run_margin():
    """抓 MI_MARGN 信用交易統計表，取大盤融資餘額(億元)與融券餘額(張)。"""
    mock = os.environ.get("MOCK_MARGIN_JSON")
    payload = (_json.load(open(mock, encoding="utf-8")) if mock
               else _fetch_json(URL_MARGIN))
    tables = payload.get("tables") or ([payload] if "data" in payload else [])
    t = None
    for cand in tables:
        title = str(cand.get("title", ""))
        fields = " ".join(str(x) for x in cand.get("fields", []))
        if "信用交易統計" in title or "今日餘額" in fields:
            t = cand
            break
    if t is None and tables:
        t = tables[0]
    if t is None:
        raise RuntimeError("MI_MARGN 找不到信用交易統計表")

    iso = _roc_title_to_iso(t.get("title", ""))
    roc = ""
    m = _re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", str(t.get("title", "")))
    if m:
        roc = f"{int(m.group(1)):03d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    fields = t.get("fields", [])
    c_today = None
    for i, f in enumerate(fields):
        if "今日餘額" in str(f):
            c_today = i
            break
    if c_today is None:
        c_today = -1   # 最後一欄通常是今日餘額

    def find_row(key):
        for r in t.get("data", []):
            if key in str(r[0]):
                return r
        return None

    def cell_num(r):
        if r is None:
            return None
        try:
            return float(str(r[c_today]).replace(",", "").strip())
        except (ValueError, IndexError):
            return None

    # 融資金額(仟元) → 億元；融券(交易單位=張)
    margin_amt = cell_num(find_row("融資金額"))       # 仟元
    short_unit = cell_num(find_row("融券(交易單位)"))  # 張
    margin_bal = round(margin_amt / 1e5, 2) if margin_amt is not None else None  # 仟元→億元
    short_bal = int(short_unit) if short_unit is not None else None

    new = pd.DataFrame([[roc, margin_bal, short_bal]], columns=MARGIN_COLS)
    new["date"] = iso or dt.date.today().isoformat()
    new = new[["date"] + MARGIN_COLS]

    total, added = merge_csv(new, CSV_MARGIN)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] 融資券：交易日 {new['date'].iloc[0]}"
          f"（融資餘額 {margin_bal} 億、融券 {short_bal} 張）；"
          f"CSV 現有 {total} 列，本次淨增 {added} 列。")


# ---------- 全市場收盤價（官方 API，每日覆蓋）----------
import json as _json
import urllib.request as _urlreq

TICKER_RE = _re.compile(r"^\d{4,6}[A-Z]?$")   # 2330 / 00981A / 6719 等


def _fetch_json(url: str):
    """帶重試的 JSON 抓取（官方 API）。用 requests 以自動跟隨 3xx 轉址（如 MI_MARGN 的 307）。"""
    last_err = None
    hdrs = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.twse.com.tw/",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=hdrs, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.json()
        except Exception as e:
            last_err = e
            wait = BACKOFF * attempt
            print(f"  第 {attempt}/{MAX_RETRIES} 次 API 連線失敗（{type(e).__name__}），"
                  f"{wait}s 後重試…", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"連線 {url} 連續 {MAX_RETRIES} 次失敗：{last_err}")


def _roc_title_to_iso(title: str) -> str:
    """從表標題抓民國日期 '115年08月21日' -> '2026-08-21'；抓不到回空字串。"""
    m = _re.search(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", str(title or ""))
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y + 1911:04d}-{mo:02d}-{d:02d}"
    return ""


def _sign_from_html(s) -> int:
    """MI_INDEX 漲跌欄含 HTML：color:red=+1、color:green=-1、其餘=0。"""
    s = str(s)
    if "color:red" in s or ">+<" in s:
        return 1
    if "color:green" in s or ">-<" in s:
        return -1
    return 0


def _find_table(tables, must_have):
    for t in tables:
        fields = " ".join(str(x) for x in t.get("fields", []))
        if all(k in fields for k in must_have):
            return t
    return None


def _col_idx(fields, *keys):
    for i, f in enumerate(fields):
        if any(k in str(f) for k in keys):
            return i
    return None


def _parse_mi_index(payload):
    """證交所 MI_INDEX：回傳 (rows, iso_date)。rows=[ticker,name,close,change,market]。"""
    tables = payload.get("tables") or []
    t = _find_table(tables, ["證券代號", "收盤價"])
    if t is None:
        raise RuntimeError("MI_INDEX 找不到『每日收盤行情』表")
    fields = t["fields"]
    ci = _col_idx(fields, "證券代號")
    cn = _col_idx(fields, "證券名稱")
    cc = _col_idx(fields, "收盤價")
    csign = _col_idx(fields, "漲跌(+/-)", "漲跌(+/–)")
    cdiff = _col_idx(fields, "漲跌價差")
    iso = _roc_title_to_iso(t.get("title", ""))
    rows = []
    for r in t.get("data", []):
        tk = str(r[ci]).strip()
        if not TICKER_RE.match(tk):
            continue
        try:
            close = float(str(r[cc]).replace(",", "").strip())
        except ValueError:
            continue
        change = ""
        if cdiff is not None:
            try:
                mag = float(str(r[cdiff]).replace(",", "").strip())
                sign = _sign_from_html(r[csign]) if csign is not None else 0
                change = mag * (sign if sign != 0 else 1)
            except ValueError:
                change = ""
        name = str(r[cn]).strip() if cn is not None else ""
        rows.append([tk, name, close, change, "TWSE"])
    return rows, iso


def _parse_tpex(payload):
    """櫃買 dailyQuotes：結構隨版本略異，盡量容錯。回傳 (rows, iso_date)。"""
    iso = ""
    data, fields = None, None
    if isinstance(payload, dict) and payload.get("tables"):
        t = _find_table(payload["tables"], ["收盤"]) or payload["tables"][0]
        fields = t.get("fields", [])
        data = t.get("data", [])
        iso = _roc_title_to_iso(t.get("title", ""))
    elif isinstance(payload, dict) and "aaData" in payload:
        data = payload["aaData"]
        fields = None
    elif isinstance(payload, list):
        data = payload
    if not data:
        return [], iso
    if fields and any("收盤" in str(f) for f in fields):
        ci = _col_idx(fields, "代號", "股票代號", "SecuritiesCompanyCode")
        cn = _col_idx(fields, "名稱", "CompanyName")
        cc = _col_idx(fields, "收盤")
        cdiff = _col_idx(fields, "漲跌")
    else:
        ci, cn, cc, cdiff = 0, 1, 2, 3   # 舊格式固定位置
    rows = []
    for r in data:
        vals = list(r.values()) if isinstance(r, dict) else r
        try:
            tk = str(vals[ci]).strip()
        except Exception:
            continue
        if not TICKER_RE.match(tk):
            continue
        try:
            close = float(str(vals[cc]).replace(",", "").replace("---", "").strip())
        except (ValueError, IndexError):
            continue
        change = ""
        if cdiff is not None:
            try:
                change = float(str(vals[cdiff]).replace(",", "").replace("+", "").strip())
            except (ValueError, IndexError):
                change = ""
        try:
            name = str(vals[cn]).strip() if cn is not None else ""
        except IndexError:
            name = ""
        rows.append([tk, name, close, change, "TPEx"])
    return rows, iso


def fetch_prices_all():
    """證交所(上市 MI_INDEX)＋櫃買(上櫃) 全市場收盤價，合併為一份。"""
    today_iso = dt.date.today().isoformat()
    all_rows, notes, iso_date = [], [], ""

    try:
        rows, iso = _parse_mi_index(_fetch_json(URL_PRICE_TWSE))
        all_rows += rows
        iso_date = iso or iso_date
        notes.append(f"上市 {len(rows)} 檔")
    except Exception as e:
        notes.append(f"上市失敗（{e}）")

    try:
        rows, iso = _parse_tpex(_fetch_json(URL_PRICE_TPEX))
        all_rows += rows
        iso_date = iso_date or iso
        notes.append(f"上櫃 {len(rows)} 檔")
    except Exception as e:
        notes.append(f"上櫃失敗（{e}）")

    if not all_rows:
        raise RuntimeError("上市與上櫃皆抓取失敗：" + "；".join(notes))

    out = pd.DataFrame(all_rows, columns=["ticker", "name", "close", "change", "market"])
    out = out.drop_duplicates(subset="ticker", keep="first").reset_index(drop=True)
    out["date"] = iso_date or today_iso     # 用資料標題的真實交易日
    notes.append(f"交易日 {iso_date or '(用執行日)'}")
    return out[["ticker", "name", "close", "change", "market", "date"]], notes


def run_prices():
    mock = os.environ.get("MOCK_PRICE_JSON")
    if mock:
        rows, iso = _parse_mi_index(_json.load(open(mock, encoding="utf-8")))
        out = pd.DataFrame(rows, columns=["ticker", "name", "close", "change", "market"])
        out["date"] = iso or dt.date.today().isoformat()
        notes = [f"mock {len(rows)} 檔，交易日 {iso}"]
    else:
        out, notes = fetch_prices_all()
    os.makedirs(os.path.dirname(CSV_PRICE), exist_ok=True)
    out.to_csv(CSV_PRICE, index=False)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] 收盤價：寫入 {len(out)} 檔"
          f"（{'、'.join(notes)}）。")


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
    # 未平倉仍用 wearn HTML；買賣超、收盤價改用證交所官方 API。三份彼此獨立。
    try:
        run_one("未平倉", URL_OI, "MOCK_HTML", pick_oi_table, normalize_oi, CSV_OI)
    except Exception as e:
        errors.append(f"未平倉：{e}")
        print(f"  ⚠ 未平倉抓取失敗：{e}", file=sys.stderr)
    # 三大法人買賣超（官方 BFI82U）
    try:
        run_fund()
    except Exception as e:
        errors.append(f"買賣超：{e}")
        print(f"  ⚠ 買賣超抓取失敗：{e}", file=sys.stderr)
    # 大盤融資融券餘額（官方 MI_MARGN）
    try:
        run_margin()
    except Exception as e:
        errors.append(f"融資券：{e}")
        print(f"  ⚠ 融資券抓取失敗：{e}", file=sys.stderr)
    # 全市場收盤價（覆蓋式，獨立處理）
    try:
        run_prices()
    except Exception as e:
        errors.append(f"收盤價：{e}")
        print(f"  ⚠ 收盤價抓取失敗：{e}", file=sys.stderr)
    if errors:
        raise RuntimeError("；".join(errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)
