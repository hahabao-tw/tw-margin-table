# -*- coding: utf-8 -*-
"""
期貨商保證金一覽表 — 自動建置腳本
Taiwan Futures Margin Dashboard builder

只用 Python 標準函式庫，方便長期維護。
資料來源：臺灣期貨交易所 (TAIFEX)
  - 保證金一覽表 CSV：股價指數類 / 股票類 / 商品類 / 匯率類
  - 收盤價（最後成交價）：TAIFEX OpenAPI 期貨每日交易行情（一般交易時段）

執行：python build.py  → 產生同目錄下的 index.html
"""

import json
import sys
import time
import datetime
import urllib.request
import urllib.error
import html as html_lib

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# 各類別保證金 CSV 下載網址（依交易所分類，順序即顯示順序，不另排序）
MARGIN_SOURCES = [
    {"key": "index", "title": "股價指數類",
     "url": "https://www.taifex.com.tw/cht/5/indexMargingDown", "type": "simple"},
    {"key": "commodity", "title": "商品類",
     "url": "https://www.taifex.com.tw/cht/5/goldMarginingDown", "type": "simple"},
    {"key": "fx", "title": "匯率類",
     "url": "https://www.taifex.com.tw/cht/5/fXMarginingDown", "type": "simple"},
    {"key": "stock", "title": "股票期貨",
     "url": "https://www.taifex.com.tw/cht/5/stockMarginingDown", "type": "stock"},
]

# 國外期貨保證金頁面（華南期貨官方網站 HTML 表格）
# 美歐交易所底下再細分 8 個交易所（用 exchange_id 參數切換）
_US_EU_AREA = "6045532e8e000000198ed97cfa994f09"
_US_EU_EXCHANGES = [
    ("us_cme",   "芝加哥商品交易所 CME",    "60a731daf000000016b2ed0bfbfd20ec"),
    ("us_cbot",  "芝加哥期貨交易所 CBOT",   "60a7353e0900000047648366b4a664cf"),
    ("us_nybot", "紐約期貨交易所 NYBOT",    "60a7379cbd000000778a9bc0d84d06af"),
    ("us_nym",   "紐約商業交易所 NYM",      "61f730abb6000000faee9e721d85c1fc"),
    ("us_cfe",   "美國CBOE期貨交易所 CFE",  "60a73665e20000004799e6f12d98a157"),
    ("us_lme",   "英國倫敦金屬交易所 LME",  "61f733b9ec0000004585baf2602b56b5"),
    ("us_eurex", "歐洲期貨交易所 EUREX",    "60a73a10240000006cef3f9b6d3f4adc"),
    ("us_life",  "倫敦國際金融交易所 LIFE", "60a738c52e0000002653c044bdddd706"),
]

_JP_AREA = "604f447ccb000000bc21641ef794eae8"
_JP_EXCHANGES = [
    ("jp_ose",   "大阪證券交易所 OSE",        "604f44abf4000000a94f2f67eaa5931b"),
    ("jp_tocom", "東工交易所 TOCOM",          "604f44ce9300000065b85128198b4caa"),
    ("jp_tfx",   "日本東京金融交易所 TFX",    "604f44fcb70000006ae45d56d15df522"),
]

_MARGIN_BASE = "https://ft.entrust.com.tw/entrustFutures/productMargin/margin.do"

FOREIGN_SOURCES = (
    [{"key": k, "title": t,
      "url": f"{_MARGIN_BASE}?area_id={_US_EU_AREA}&exchange_id={x}&category_id="}
     for (k, t, x) in _US_EU_EXCHANGES]
    + [{"key": k, "title": t,
        "url": f"{_MARGIN_BASE}?area_id={_JP_AREA}&exchange_id={x}&category_id="}
       for (k, t, x) in _JP_EXCHANGES]
    + [
        {"key": "sgx", "title": "新加坡期貨交易所 SGX",
         "url": f"{_MARGIN_BASE}?area_id=604f4550500000006688bebcfb52416a"},
        {"key": "hkf", "title": "香港交易所 HKF",
         "url": f"{_MARGIN_BASE}?area_id=604f45b55f0000001d713878698680a8"},
    ]
)

# 國外期貨商品名稱覆寫（key = 原始名稱, value = 顯示名稱）
FOREIGN_NAME_OVERRIDE = {
    "(USD)日經225": "日經225",
}


# 幣別中文 → 代碼
FOREIGN_CURRENCY_MAP = {
    "美金": "USD", "日圓": "JPY", "港幣": "HKD",
    "新幣": "SGD", "歐元": "EUR", "英磅": "GBP",
    "人民幣": "CNY",
}

# 期貨每日交易行情（OpenAPI，JSON，最新交易日、一般交易時段）
DAILY_REPORT_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"

# 股票期貨換算保證金的「契約乘數」
MULT_STOCK = 2000     # 一般股票期貨：2,000 股/口
MULT_MINI = 100       # 小型股票期貨：100 股/口
MULT_ETF = 10000      # ETF 股票期貨：10,000 受益權單位/口

# 幣別：預設台幣 (TWD)。以下為非台幣商品（依商品名稱比對）
# 商品類（黃金、原油以美金計價；臺幣黃金為台幣）
COMMODITY_CURRENCY = {
    "黃金期貨": "USD",
    "黃金選擇權風險保證金(A)值": "USD",
    "黃金選擇權風險保證金(B)值": "USD",
    "美國原油期貨": "USD",
    # 「臺幣黃金期貨」走預設 TWD
}

# 連線標頭（TAIFEX 偶爾需要 Referer / Origin，否則回傳 HTML 而非 CSV）
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; margin-dashboard/1.0; +https://github.com/)",
    "Referer": "https://www.taifex.com.tw/cht/5/indexMarging",
    "Origin": "https://www.taifex.com.tw",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

ENCODINGS = ["utf-8-sig", "big5", "cp950", "utf-8"]


# ---------------------------------------------------------------------------
# 抓取工具
# ---------------------------------------------------------------------------

def fetch_bytes(url, headers=None, retries=3, timeout=30):
    """抓取網頁原始 bytes，含簡單重試。"""
    last_err = None
    h = dict(COMMON_HEADERS)
    if headers:
        h.update(headers)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  [warn] 抓取失敗 ({attempt}/{retries}) {url} -> {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"無法抓取 {url}: {last_err}")


def decode_text(raw):
    """依序嘗試多種編碼解碼（TAIFEX CSV 多為 big5/MS950）。"""
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def looks_like_html(text):
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------

def _clean_num(s):
    """'471,000' / '13.50%' -> float；無法解析回 None。"""
    if s is None:
        return None
    s = s.strip().strip('"').replace(",", "")
    if s in ("", "-", "—"):
        return None
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if pct else v


def _split_csv_line(line):
    """簡易 CSV 切欄，支援雙引號包住的欄位。"""
    out, cur, in_q = [], [], False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [c.strip().strip('"').strip() for c in out]


def find_update_date(text):
    """從 CSV 內找『更新日期』。"""
    for line in text.splitlines():
        if "更新日期" in line:
            # 可能是 '更新日期:2026/06/18' 或 ' , ,更新日期,2026/05/27'
            for token in line.replace(":", ",").split(","):
                token = token.strip()
                if any(c.isdigit() for c in token) and ("/" in token or "-" in token):
                    return token
    return ""


def parse_simple_margin(text):
    """
    解析『股價指數類 / 商品類 / 匯率類』CSV。
    回傳 [{name, original_margin}], 取『原始保證金』(第四欄、三個數字中的最後一個)。
    """
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = _split_csv_line(line)
        if len(fields) < 4:
            continue
        name = fields[0].strip()
        if not name or "商品別" in name or "更新日期" in name:
            continue
        # 選擇權類流動性較差，且「風險保證金」非每口固定保證金，
        # 不適合用於「可下單口數」換算，故一律排除。
        if "選擇權" in name:
            continue
        n1, n2, n3 = _clean_num(fields[1]), _clean_num(fields[2]), _clean_num(fields[3])
        # 一筆有效資料：商品名 + 結算/維持/原始 三個數字
        if n1 is None or n2 is None or n3 is None:
            continue
        rows.append({"name": name, "original_margin": n3})
    return rows


def parse_stock_margin(text):
    """
    解析『股票類』CSV。
    遇到「選擇權契約」段落即停止（個股選擇權不列入本表）。

    個股期貨欄位（9欄）：
      序號, 英文代碼, 證券代號, 中文簡稱, 標的證券, 保證金所屬級距,
      結算比例, 維持比例, 原始保證金適用比例
    ETF 期貨欄位（8欄）：
      序號, 英文代碼, 證券代號, 中文簡稱, 標的證券,
      結算保證金, 維持保證金, 原始保證金（直接是整數）

    回傳 [{code, ucode, name, ratio, fixed_margin, kind}]
      kind: 'stock' | 'mini' | 'etf'
      ratio: 僅 stock/mini 有效（0~1），etf 為 None
      fixed_margin: 僅 etf 有效（整數），stock/mini 為 None
    """
    rows = []
    section = "stock"
    for line in text.splitlines():
        if not line.strip():
            continue
        if "選擇權契約" in line:
            break
        # 區段標題判斷（只有含「標的證券為」的說明行才切換，避免資料列名稱含 ETF 被誤判）
        if "標的證券為" in line:
            if "受益" in line or "ＥＴＦ" in line or "ETF" in line:
                section = "etf"
            elif "股票" in line:
                section = "stock"
            continue
        fields = _split_csv_line(line)
        seq = fields[0].strip() if fields else ""
        if not seq.isdigit():
            continue
        code  = fields[1].strip() if len(fields) > 1 else ""
        ucode = fields[2].strip() if len(fields) > 2 else ""
        name  = fields[3].strip() if len(fields) > 3 else ""
        if not code:
            continue

        if section == "etf":
            # ETF：8欄，第8欄（index 7）= 原始保證金（整數）
            if len(fields) < 8:
                continue
            fixed = _clean_num(fields[7])
            if fixed is None:
                continue
            rows.append({"code": code, "ucode": ucode, "name": name,
                         "ratio": None, "fixed_margin": int(fixed), "kind": "etf"})
        else:
            # 個股：9欄，第9欄（index 8）= 原始保證金適用比例
            if len(fields) < 9:
                continue
            ratio = _clean_num(fields[8])
            if ratio is None:
                continue
            kind = "mini" if name.startswith("小型") else "stock"
            rows.append({"code": code, "ucode": ucode, "name": name,
                         "ratio": ratio, "fixed_margin": None, "kind": kind})
    return rows


# ---------------------------------------------------------------------------
# 收盤價（最後成交價，一般交易時段）
# ---------------------------------------------------------------------------

# OpenAPI 欄位名稱候選（中英文都試，增加相容性）
KEY_CONTRACT = ["Contract", "契約", "商品代號", "ProductId", "product_id"]
KEY_MONTH = ["ContractMonth(Week)", "到期月份(週別)", "ContractMonth",
             "ContractMonthWeek", "到期月份"]
KEY_LAST = ["Last", "最後成交價", "ClosePrice", "Close"]
KEY_SETTLE = ["SettlementPrice", "結算價", "Settlement"]
KEY_SESSION = ["TradingSession", "交易時段", "Session"]
KEY_NAME = ["ProductName", "中文簡稱", "商品名稱", "Name"]
KEY_VOLUME = ["Volume", "成交量", "TradingVolume", "TradeVolume"]


def _get(rec, keys):
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


def get_close_prices():
    """
    回傳 ({code: price}, {code: volume})。
    取一般交易時段、各商品近月（最早到期月份）有效成交價與成交量。
    無最後成交價時退而求其次用結算價。
    """
    try:
        raw = fetch_bytes(DAILY_REPORT_URL,
                          headers={"Accept": "application/json"})
        data = json.loads(decode_text(raw))
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 每日行情抓取/解析失敗：{e}", file=sys.stderr)
        return {}, {}

    if not isinstance(data, list) or not data:
        print("  [warn] 每日行情回傳格式非預期（非陣列或空）", file=sys.stderr)
        return {}, {}

    print(f"  每日行情筆數：{len(data)}；首筆欄位：{list(data[0].keys())}",
          file=sys.stderr)

    # best[code] = (month_str, price, volume)
    best = {}
    name_best = {}
    kept = 0
    for rec in data:
        session = _get(rec, KEY_SESSION)
        if session is not None:
            s = str(session)
            if not ("一般" in s or s in ("0", "Position", "REGULAR", "Regular", "position")):
                continue
        code = _get(rec, KEY_CONTRACT)
        if not code:
            continue
        code = str(code).strip()
        month = str(_get(rec, KEY_MONTH) or "")
        price = _clean_num(str(_get(rec, KEY_LAST) or ""))
        if price is None:
            price = _clean_num(str(_get(rec, KEY_SETTLE) or ""))
        if price is None or price <= 0:
            continue
        vol = _clean_num(str(_get(rec, KEY_VOLUME) or "")) or 0
        kept += 1
        if code not in best or month < best[code][0]:
            best[code] = (month, price, int(vol))
        name = _get(rec, KEY_NAME)
        if name:
            name = str(name).strip()
            if name not in name_best or month < name_best[name][0]:
                name_best[name] = (month, price, int(vol))

    price_map = {c: v[1] for c, v in best.items()}
    vol_map   = {c: v[2] for c, v in best.items()}
    for n, v in name_best.items():
        price_map.setdefault(n, v[1])
        vol_map.setdefault(n, v[2])
    print(f"  收盤價可用商品數：{len(best)}（有效列 {kept}）", file=sys.stderr)
    return price_map, vol_map


# ---------------------------------------------------------------------------
# 幣別
# ---------------------------------------------------------------------------

def currency_for_simple(category_key, name):
    if category_key == "commodity":
        return COMMODITY_CURRENCY.get(name, "TWD")
    if category_key == "fx":
        # 匯率類以「貨幣對後者」為計價幣別：A兌B期貨
        if "兌美元" in name:
            return "USD"
        if "兌人民幣" in name:
            return "CNY"
        if "兌日圓" in name or "兌日元" in name:
            return "JPY"
        if "兌歐元" in name:
            return "EUR"
        return "TWD"
    return "TWD"


# ---------------------------------------------------------------------------
# 組裝資料
# ---------------------------------------------------------------------------

def build_dataset():
    sections = []
    update_dates = {}

    print("抓取每日行情（收盤價＋成交量）…", file=sys.stderr)
    price_map, vol_map = get_close_prices()

    for src in MARGIN_SOURCES:
        print(f"抓取 {src['title']} …", file=sys.stderr)
        try:
            raw = fetch_bytes(src["url"])
            text = decode_text(raw)
            if looks_like_html(text):
                raise RuntimeError("回傳為 HTML 而非 CSV（可能被擋）")
        except Exception as e:  # noqa: BLE001
            print(f"  [error] {src['title']} 抓取失敗：{e}", file=sys.stderr)
            sections.append({"key": src["key"], "title": src["title"],
                             "error": str(e), "rows": [], "kind": src["type"]})
            continue

        update_dates[src["key"]] = find_update_date(text)

        if src["type"] == "simple":
            parsed = parse_simple_margin(text)
            rows = []
            for r in parsed:
                cur = currency_for_simple(src["key"], r["name"])
                rows.append({
                    "name": r["name"],
                    "margin": r["original_margin"],
                    "currency": cur,
                })
            sections.append({"key": src["key"], "title": src["title"],
                             "kind": "simple", "rows": rows,
                             "update": update_dates[src["key"]]})

        else:  # stock
            parsed = parse_stock_margin(text)
            stock_rows, etf_rows = [], []
            matched = 0
            for r in parsed:
                if r["kind"] == "etf":
                    # ETF：直接用固定保證金，不需要收盤價，保持原始順序
                    etf_rows.append({
                        "code": r["code"],
                        "ucode": r["ucode"],
                        "name": r["name"],
                        "ratio": None,
                        "kind": "etf",
                        "mult": MULT_ETF,
                        "price": None,
                        "volume": 0,
                        "margin": r["fixed_margin"],
                        "currency": "TWD",
                    })
                else:
                    # 個股/小型：收盤價 × 乘數 × 比例
                    mult = MULT_MINI if r["kind"] == "mini" else MULT_STOCK
                    price = price_map.get(r["code"]) or price_map.get(r["name"])
                    vol   = vol_map.get(r["code"])   or vol_map.get(r["name"]) or 0
                    if price is not None:
                        matched += 1
                        margin = round(price * mult * r["ratio"])
                    else:
                        margin = None
                    stock_rows.append({
                        "code": r["code"],
                        "ucode": r["ucode"],
                        "name": r["name"],
                        "ratio": r["ratio"],
                        "kind": r["kind"],
                        "mult": mult,
                        "price": price,
                        "volume": vol,
                        "margin": margin,
                        "currency": "TWD",
                    })
            # 個股：成交量降冪，次之代號升冪
            stock_rows.sort(key=lambda x: (-x["volume"], x["ucode"]))
            # ETF：保持 CSV 原始順序（不排序）
            print(f"  個股期貨 {len(stock_rows)} 檔（對到收盤價 {matched} 檔），"
                  f"ETF {len(etf_rows)} 檔", file=sys.stderr)
            sections.append({"key": "stock", "title": "股票期貨",
                             "kind": "stock", "rows": stock_rows,
                             "update": update_dates[src["key"]],
                             "matched": matched})
            sections.append({"key": "etf", "title": "ETF股票期貨",
                             "kind": "etf_fixed", "rows": etf_rows,
                             "update": update_dates[src["key"]],
                             "search_id": "etfSearch",
                             "clear_id":  "etfClear",
                             "noresult_id": "etfNoResult"})

    # 國外期貨資料
    foreign_sections = build_foreign_dataset()

    return {
        "sections": sections,
        "foreign_sections": foreign_sections,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC",
        "generated_tw": (datetime.datetime.utcnow() +
                         datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
    }


def build_foreign_dataset():
    """爬取華南期貨網站的國外期貨保證金 HTML 表格。"""
    import re
    sections = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; margin-dashboard/1.0)",
        "Referer": "https://ft.entrust.com.tw/",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    seen_signatures = {}
    for src in FOREIGN_SOURCES:
        print(f"抓取國外 {src['title']} …", file=sys.stderr)
        req_headers = dict(headers)
        req_headers["Referer"] = src["url"]
        try:
            raw = fetch_bytes(src["url"], headers=req_headers)
            text = decode_text(raw)
        except Exception as e:  # noqa: BLE001
            print(f"  [error] {src['title']} 抓取失敗：{e}", file=sys.stderr)
            sections.append({"key": src["key"], "title": src["title"],
                             "error": str(e), "rows": [], "groups": []})
            continue

        # 從 HTML 中解析 <table> 的 <tr> 列
        # 表格欄位：商品分類 | 商品名稱 | 商品代碼 | 原始保證金 | 維持保證金 | 幣別
        rows = []
        tr_blocks = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.S | re.I)
        def strip_tags(s):
            s = re.sub(r'<[^>]+>', '', s).strip()
            s = s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
                 .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
            return s
        for tr in tr_blocks:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S | re.I)
            if len(cells) < 6:
                continue
            cat    = strip_tags(cells[0])
            name   = strip_tags(cells[1])
            code   = strip_tags(cells[2])
            orig   = strip_tags(cells[3]).replace(",", "")
            cur_zh = strip_tags(cells[5])
            if not name or not code or not orig.isdigit():
                continue
            cur = FOREIGN_CURRENCY_MAP.get(cur_zh, cur_zh)
            name = FOREIGN_NAME_OVERRIDE.get(name, name)
            rows.append({"cat": cat, "name": name, "code": code,
                         "margin": int(orig), "currency": cur})

        # 防呆：偵測子交易所是否回傳「完全相同」的資料
        # （代表 exchange_id 參數未生效，伺服器回了預設頁）
        is_sub = src["key"].startswith("us_") or src["key"].startswith("jp_")
        sig = tuple(sorted((r["code"], r["margin"]) for r in rows))
        if is_sub and sig in seen_signatures and rows:
            print(f"  [warn] {src['title']} 與 {seen_signatures[sig]} 資料相同，"
                  f"exchange_id 可能未生效；略過避免重複。", file=sys.stderr)
            continue
        if rows:
            seen_signatures[sig] = src["title"]

        # 按商品分類分組
        groups = {}
        group_order = []
        for r in rows:
            g = r["cat"]
            if g not in groups:
                groups[g] = []
                group_order.append(g)
            groups[g].append(r)

        print(f"  {src['title']}：{len(rows)} 筆", file=sys.stderr)
        # 子交易所若無資料則不顯示空白區塊；SGX/HKF 即使空也保留
        if not rows and is_sub:
            print(f"  {src['title']} 無資料，略過。", file=sys.stderr)
            continue
        sections.append({"key": src["key"], "title": src["title"],
                         "groups": [{"cat": g, "rows": groups[g]} for g in group_order],
                         "rows": rows, "error": None})
    return sections


# ---------------------------------------------------------------------------
# HTML 產生
# ---------------------------------------------------------------------------

def esc(s):
    return html_lib.escape(str(s))


def fmt_int(n):
    if n is None:
        return "—"
    return f"{int(round(n)):,}"


def fmt_price(p):
    if p is None:
        return "—"
    # 股價可能有小數
    if abs(p - round(p)) < 1e-9:
        return f"{int(round(p)):,}"
    return f"{p:,.2f}"


def fmt_pct(r):
    return f"{r * 100:.2f}%"


CURRENCY_LABEL = {"TWD": "NT$", "USD": "US$", "JPY": "¥", "CNY": "¥CN", "EUR": "€"}


# 預設展開的分類（其餘預設摺疊）
OPEN_BY_DEFAULT = {"index"}


def render_simple_section(sec):
    is_open = " open" if sec["key"] in OPEN_BY_DEFAULT else ""
    sid = f'cat-{sec["key"]}'
    parts = [f'<details class="cat" id="{sid}" data-cat="{esc(sec["key"])}"{is_open}>']
    parts.append(f'<summary><span class="cat-name">{esc(sec["title"])}</span>'
                 f'<span class="upd">更新：{esc(sec.get("update") or "—")}</span>'
                 f'<span class="chev" aria-hidden="true"></span></summary>')
    parts.append('<div class="catbody">')
    if sec.get("error"):
        parts.append(f'<p class="err">資料暫時無法取得：{esc(sec["error"])}</p></div></details>')
        return "".join(parts)

    # 是否整段同一幣別
    currencies = {r["currency"] for r in sec["rows"]}
    if len(currencies) == 1:
        cur = next(iter(currencies))
        parts.append(f'<p class="curnote">單位：{esc(CURRENCY_LABEL.get(cur, cur))}（{esc(cur)}）</p>')

    parts.append('<div class="tw"><table>')
    parts.append('<thead><tr>'
                 '<th class="l">商品別</th>'
                 '<th class="r">原始保證金</th>'
                 '<th class="r">可下單口數</th>'
                 '</tr></thead><tbody>')
    for r in sec["rows"]:
        cur = r["currency"]
        cur_badge = "" if cur == "TWD" else f'<span class="cur">{esc(cur)}</span>'
        parts.append(
            '<tr class="mrow" '
            f'data-margin="{r["margin"] if r["margin"] is not None else ""}" '
            f'data-currency="{esc(cur)}">'
            f'<td class="l">{esc(r["name"])}{cur_badge}</td>'
            f'<td class="r num">{fmt_int(r["margin"])}</td>'
            f'<td class="r num lots">—</td>'
            '</tr>')
    parts.append('</tbody></table></div></div></details>')
    return "".join(parts)


def render_etf_section(sec, search_id="etfSearch", clear_id="etfClear", noresult_id="etfNoResult"):
    """ETF 股票期貨：直接顯示固定原始保證金，不需要收盤價，保持原始排序。"""
    is_open = " open" if "etf" in OPEN_BY_DEFAULT else ""
    sid = f'cat-{sec["key"]}'
    parts = [f'<details class="cat" id="{sid}" data-cat="{esc(sec["key"])}"{is_open}>']
    parts.append(f'<summary><span class="cat-name">{esc(sec["title"])}</span>'
                 f'<span class="upd">更新：{esc(sec.get("update") or "—")}</span>'
                 f'<span class="chev" aria-hidden="true"></span></summary>')
    parts.append('<div class="catbody">')
    if not sec.get("rows"):
        parts.append('<p class="curnote">目前無資料。</p></div></details>')
        return "".join(parts)
    # 搜尋列
    parts.append(f'<div class="searchbar">'
                 f'<svg class="sicon" viewBox="0 0 24 24" aria-hidden="true">'
                 f'<circle cx="11" cy="11" r="7"></circle>'
                 f'<line x1="21" y1="21" x2="16.5" y2="16.5"></line></svg>'
                 f'<input id="{search_id}" type="search" inputmode="search" '
                 f'autocomplete="off" placeholder="搜尋：名稱或代號（例：元大、0050）" />'
                 f'<button type="button" id="{clear_id}" class="sclear" '
                 f'aria-label="清除搜尋">×</button></div>')
    parts.append(f'<p class="curnote">單位：NT$（TWD）。共 {len(sec["rows"])} 檔，依交易所原始順序排列。</p>')
    parts.append('<div class="tw"><table class="stock">')
    parts.append('<thead><tr>'
                 '<th class="l">商品（標的）</th>'
                 '<th class="r">原始保證金</th>'
                 '<th class="r">可下單口數</th>'
                 '</tr></thead><tbody>')
    for r in sec["rows"]:
        search_key = f'{r["name"]} {r["ucode"]} {r["code"]}'.lower()
        parts.append(
            '<tr class="mrow srow" '
            f'data-margin="{r["margin"] if r["margin"] is not None else ""}" '
            f'data-search="{esc(search_key)}" '
            'data-currency="TWD">'
            f'<td class="l">{esc(r["name"])}'
            f'<span class="ucode">{esc(r["ucode"])}・{esc(r["code"])}</span></td>'
            f'<td class="r num">{fmt_int(r["margin"])}</td>'
            f'<td class="r num lots">—</td>'
            '</tr>')
    parts.append('</tbody></table>')
    parts.append(f'<p class="noresult" id="{noresult_id}" hidden>找不到符合的 ETF 期貨。</p>')
    parts.append('</div></div></details>')
    return "".join(parts)


def render_stock_section(sec, search_id="stockSearch", clear_id="stockClear", noresult_id="stockNoResult"):
    is_open = " open" if "stock" in OPEN_BY_DEFAULT else ""
    sid = f'cat-{sec["key"]}'
    parts = [f'<details class="cat" id="{sid}" data-cat="{esc(sec["key"])}"{is_open}>']
    matched = sec.get("matched", 0)
    total = len(sec["rows"])
    parts.append(f'<summary><span class="cat-name">{esc(sec["title"])}</span>'
                 f'<span class="upd">更新：{esc(sec.get("update") or "—")}</span>'
                 f'<span class="chev" aria-hidden="true"></span></summary>')
    parts.append('<div class="catbody">')
    if sec.get("error"):
        parts.append('<p class="err">資料暫時無法取得：' + esc(sec["error"]) + '</p></div></details>')
        return "".join(parts)
    # 股票期貨專屬搜尋列
    parts.append(f'<div class="searchbar">'
                 f'<svg class="sicon" viewBox="0 0 24 24" aria-hidden="true">'
                 f'<circle cx="11" cy="11" r="7"></circle>'
                 f'<line x1="21" y1="21" x2="16.5" y2="16.5"></line></svg>'
                 f'<input id="{search_id}" type="search" inputmode="search" '
                 f'autocomplete="off" placeholder="搜尋：名稱或代號（例：台積電、2330）" />'
                 f'<button type="button" id="{clear_id}" class="sclear" '
                 f'aria-label="清除搜尋">×</button></div>')
    parts.append('<p class="curnote">單位：NT$（TWD）。'
                 '原始保證金 = 收盤價 × 契約乘數 × 原始保證金適用比例。'
                 f'已對到收盤價 {matched}/{total} 檔。'
                 '成交量為近月一般時段，降冪排序。</p>')
    parts.append('<div class="tw"><table class="stock">')
    parts.append('<thead><tr>'
                 '<th class="l">商品（標的）</th>'
                 '<th class="r">比例</th>'
                 '<th class="r">原始保證金</th>'
                 '<th class="r">可下單口數</th>'
                 '<th class="r">成交量</th>'
                 '</tr></thead><tbody>')
    kind_label = {"stock": "", "mini": "小", "etf": "ETF"}
    for r in sec["rows"]:
        tag = kind_label.get(r["kind"], "")
        tagspan = f'<span class="kind">{esc(tag)}</span>' if tag else ""
        # 收盤價獨立成第 2 行灰色備註，避免手機被截斷
        price_note = f'<span class="ucode">收盤 {esc(fmt_price(r["price"]))}</span>' if r["price"] is not None else ""
        sub = f'<span class="ucode">{esc(r["ucode"])}・×{r["mult"]:,}</span>{price_note}'
        search_key = f'{r["name"]} {r["ucode"]} {r["code"]}'.lower()
        vol_str = f'{r["volume"]:,}' if r.get("volume") else "—"
        parts.append(
            '<tr class="mrow srow" '
            f'data-margin="{r["margin"] if r["margin"] is not None else ""}" '
            f'data-search="{esc(search_key)}" '
            'data-currency="TWD">'
            f'<td class="l">{esc(r["name"])}{tagspan}<br>{sub}</td>'
            f'<td class="r num">{fmt_pct(r["ratio"])}</td>'
            f'<td class="r num">{fmt_int(r["margin"])}</td>'
            f'<td class="r num lots">—</td>'
            f'<td class="r num vol">{vol_str}</td>'
            '</tr>')
    parts.append('</tbody></table>')
    parts.append(f'<p class="noresult" id="{noresult_id}" hidden>找不到符合的股票期貨。</p>')
    parts.append('</div></div></details>')
    return "".join(parts)


def render_html(ds):
    # 國內區塊
    body = []
    for sec in ds["sections"]:
        if sec["kind"] == "etf_fixed":
            search_id   = sec.get("search_id",   "etfSearch")
            clear_id    = sec.get("clear_id",    "etfClear")
            noresult_id = sec.get("noresult_id", "etfNoResult")
            body.append(render_etf_section(sec, search_id, clear_id, noresult_id))
        elif sec["kind"] == "stock":
            search_id    = sec.get("search_id", "stockSearch")
            clear_id     = search_id.replace("Search", "Clear")
            noresult_id  = sec.get("noresult_id", "stockNoResult")
            body.append(render_stock_section(sec, search_id, clear_id, noresult_id))
        else:
            body.append(render_simple_section(sec))
    domestic_html = "\n".join(body)

    # 國外區塊
    foreign_body = []
    for sec in ds.get("foreign_sections", []):
        foreign_body.append(render_foreign_section(sec))
    foreign_html = "\n".join(foreign_body)

    # 浮動快捷選單項目（國內）
    domestic_nav = ['<div class="fab-grid">']
    for sec in ds["sections"]:
        sid = f'cat-{sec["key"]}'
        domestic_nav.append(f'<button class="fab-item" data-target="{sid}">{esc(sec["title"])}</button>')
    domestic_nav.append('</div>')

    # 浮動快捷選單項目（國外）
    foreign_nav = ['<div class="fab-grid">']
    for sec in ds.get("foreign_sections", []):
        sid = f'fcat-{sec["key"]}'
        foreign_nav.append(f'<button class="fab-item" data-target="{sid}">{esc(sec["title"])}</button>')
    foreign_nav.append('</div>')

    return (HTML_TEMPLATE
            .replace("{{DOMESTIC_SECTIONS}}", domestic_html)
            .replace("{{FOREIGN_SECTIONS}}", foreign_html)
            .replace("{{DOMESTIC_NAV}}", "\n".join(domestic_nav))
            .replace("{{FOREIGN_NAV}}", "\n".join(foreign_nav))
            .replace("{{GEN_TW}}", esc(ds["generated_tw"]))
            .replace("{{GEN_UTC}}", esc(ds["generated_at"])))


def render_foreign_section(sec):
    """國外期貨交易所區塊。"""
    sid = f'fcat-{sec["key"]}'
    parts = [f'<details class="cat" id="{sid}" data-cat="{esc(sec["key"])}">']
    parts.append(f'<summary><span class="cat-name">{esc(sec["title"])}</span>'
                 f'<span class="chev" aria-hidden="true"></span></summary>')
    parts.append('<div class="catbody">')
    if sec.get("error"):
        parts.append(f'<p class="err">資料暫時無法取得：{esc(sec["error"])}</p></div></details>')
        return "".join(parts)
    if not sec.get("groups"):
        parts.append('<p class="curnote">目前無資料。</p></div></details>')
        return "".join(parts)

    for grp in sec["groups"]:
        parts.append(f'<p class="grp-label">{esc(grp["cat"])}</p>')
        parts.append('<div class="tw"><table>')
        parts.append('<thead><tr>'
                     '<th class="l">商品名稱</th>'
                     '<th class="r">原始保證金</th>'
                     '<th class="r">幣別</th>'
                     '</tr></thead><tbody>')
        for r in grp["rows"]:
            cur_label = r["currency"]  # 只顯示代碼，如 USD、JPY
            search_key = f'{r["name"]} {r["code"]}'.lower()
            parts.append(
                f'<tr class="mrow frow" '
                f'data-search="{esc(search_key)}" '
                f'data-margin="{r["margin"]}">'
                f'<td class="l">{esc(r["name"])}'
                f'<span class="ucode">{esc(r["code"])}</span></td>'
                f'<td class="r num">{r["margin"]:,}</td>'
                f'<td class="r">'
                f'<span class="cur-badge cur-{esc(r["currency"].lower())}">'
                f'{esc(cur_label)}</span></td>'
                f'</tr>')
        parts.append('</tbody></table></div>')
    parts.append('</div></details>')
    return "".join(parts)


# HTML 模板（CSS / JS 內嵌，單檔即可放上 GitHub Pages）
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>華南期貨商品保證金一覽</title>
<style>
  :root{
    --fs:16px;
    --bg:#0a0e17; --bg2:#0e1422;
    --card:#121a2b; --card2:#0f1626;
    --line:#23304a; --line2:#2e3e5e;
    --fg:#e8eef9; --muted:#8593ab;
    --accent:#35e0d6; --accent-b:#4ea1ff; --accent-v:#a78bfa;
    --warn:#ffb454;
    --th:#0e1626; --zebra:#0d1320;
    --mono:"SF Mono","JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
  }
  @media(prefers-color-scheme:light){
    :root{
      --bg:#eef2f8; --bg2:#e7ecf5; --card:#fff; --card2:#f7f9fd;
      --line:#dde4ef; --line2:#cfd9ea; --fg:#16203a; --muted:#5c6a85;
      --accent:#0bb3a8; --accent-b:#2f7ff0; --accent-v:#7c5cff;
      --warn:#c97a1a; --th:#eef3fb; --zebra:#f5f8fd;
    }
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;color:var(--fg);
    font-family:-apple-system,"Segoe UI",Roboto,"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;
    font-size:var(--fs);line-height:1.5;-webkit-text-size-adjust:100%;}
  body{
    background:
      radial-gradient(1100px 520px at 50% -8%,rgba(78,161,255,.10),transparent 60%),
      radial-gradient(820px 420px at 92% 4%,rgba(167,139,250,.08),transparent 55%),
      radial-gradient(820px 420px at 6% 8%,rgba(53,224,214,.07),transparent 55%),
      linear-gradient(180deg,var(--bg) 0%,var(--bg2) 100%);
    background-attachment:fixed;min-height:100vh;
  }
  .wrap{max-width:840px;margin:0 auto;padding:0 12px 100px;}

  /* ---- Header ---- */
  header.bar{position:sticky;top:0;z-index:30;
    background:linear-gradient(180deg,rgba(12,18,32,.94),rgba(12,18,32,.80));
    border-bottom:1px solid var(--line);padding:9px 12px 10px;margin:0 -12px 0;
    backdrop-filter:saturate(1.3) blur(10px);-webkit-backdrop-filter:saturate(1.3) blur(10px);}
  @media(prefers-color-scheme:light){
    header.bar{background:linear-gradient(180deg,rgba(255,255,255,.96),rgba(255,255,255,.82));}}
  .bar-top{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
  .bar h1{font-size:1.05rem;margin:0;display:flex;align-items:center;gap:8px;
    letter-spacing:.4px;font-weight:700;flex:1;}
  .bar h1 .logo{width:20px;height:20px;flex:0 0 20px;}
  .bar h1 .ttl{
    font-size:calc(var(--fs) + 4px);
    background:linear-gradient(92deg,var(--accent),var(--accent-b) 55%,var(--accent-v));
    -webkit-background-clip:text;background-clip:text;color:transparent;}

  /* Social icons */
  .social{display:flex;gap:10px;align-items:center;}
  @keyframes social-pulse{0%,100%{box-shadow:0 0 0 0 rgba(53,224,214,0);}50%{box-shadow:0 0 0 5px rgba(53,224,214,.18);}}
  .social a{display:flex;align-items:center;justify-content:center;
    width:30px;height:30px;border-radius:8px;color:var(--muted);
    transition:color .15s,background .15s;text-decoration:none;
    animation:social-pulse 2.8s ease-in-out infinite;}
  .social a:nth-child(2){animation-delay:1.4s;}
  .social a:hover{animation:none;color:var(--fg);background:rgba(255,255,255,.1);box-shadow:0 0 0 2px var(--accent);}
  .social svg{width:20px;height:20px;display:block;}

  /* 分頁切換 */
  .tabs{display:flex;gap:6px;margin:10px 0 0;}
  .tab-btn{flex:1;border:1px solid var(--line2);border-radius:10px;
    background:transparent;color:var(--muted);font-size:.85rem;font-weight:600;
    padding:7px 4px;cursor:pointer;transition:all .18s;letter-spacing:.3px;}
  .tab-btn.active{background:linear-gradient(135deg,rgba(53,224,214,.18),rgba(78,161,255,.18));
    border-color:var(--accent);color:var(--fg);box-shadow:0 0 12px rgba(53,224,214,.15);}
  .tab-btn:hover:not(.active){background:rgba(78,161,255,.08);color:var(--fg);}

  /* controls */
  .controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:9px;}
  .fsgroup{display:flex;gap:0;border:1px solid var(--line2);border-radius:10px;overflow:hidden;}
  .fsbtn{border:0;border-right:1px solid var(--line2);background:transparent;color:var(--fg);
    padding:6px 10px;font-size:.9rem;cursor:pointer;min-width:38px;transition:background .15s;}
  .fsbtn:last-child{border-right:0;}
  .fsbtn:hover{background:rgba(78,161,255,.12);}
  .fsbtn:active{transform:scale(.95);}
  .fsbtn.mid{font-weight:700;}
  .calc{flex:1 1 200px;display:flex;align-items:center;gap:7px;
    border:1px solid var(--line2);border-radius:12px;padding:5px 10px;
    background:var(--card2);transition:border-color .18s,box-shadow .18s;}
  .calc:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px rgba(53,224,214,.16);}
  .calc label{color:var(--muted);font-size:.78rem;white-space:nowrap;}
  .calc input{flex:1;min-width:0;border:0;background:transparent;color:var(--fg);
    font-size:1rem;font-family:var(--mono);font-variant-numeric:tabular-nums;
    outline:none;text-align:center;letter-spacing:.5px;}
  .calc .ccy{color:var(--accent);font-size:.78rem;font-weight:700;}
  .stepper{border:0;background:rgba(78,161,255,.15);color:var(--accent-b);
    border-radius:6px;width:26px;height:26px;font-size:1rem;font-weight:700;
    cursor:pointer;display:flex;align-items:center;justify-content:center;
    flex:0 0 26px;transition:background .15s,transform .1s;line-height:1;}
  .stepper:hover{background:rgba(78,161,255,.28);}
  .stepper:active{transform:scale(.9);}
  .vcounter{display:inline-block;margin-top:8px;font-size:.7rem;color:var(--muted);
    padding:3px 10px;border:1px solid var(--line);border-radius:12px;background:var(--card2);
    font-family:var(--mono);letter-spacing:.3px;}
  .hint{color:var(--muted);font-size:.73rem;margin:6px 2px 0;line-height:1.45;}

  /* 分頁內容 */
  .tab-panel{display:none;padding-top:14px;}
  .tab-panel.active{display:block;}

  /* 分類區塊 */
  details.cat{background:linear-gradient(180deg,var(--card),var(--card2));
    border:1px solid var(--line);border-radius:14px;margin:0 0 12px;overflow:hidden;
    box-shadow:0 8px 24px -18px rgba(0,0,0,.6);}
  details.cat[open]{border-color:var(--line2);}
  details.cat>summary{list-style:none;cursor:pointer;user-select:none;
    display:flex;align-items:center;gap:10px;
    padding:12px 14px;font-size:.95rem;font-weight:700;letter-spacing:.3px;}
  details.cat>summary::-webkit-details-marker{display:none;}
  summary .cat-name{position:relative;padding-left:12px;}
  summary .cat-name::before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
    width:3px;height:1em;border-radius:3px;
    background:linear-gradient(180deg,var(--accent),var(--accent-b));
    box-shadow:0 0 8px rgba(53,224,214,.5);}
  summary .upd{margin-left:auto;color:var(--muted);font-size:.68rem;font-weight:400;white-space:nowrap;}
  summary .chev{width:14px;height:14px;flex:0 0 14px;position:relative;}
  summary .chev::before,summary .chev::after{content:"";position:absolute;top:6px;width:8px;height:2px;
    border-radius:2px;background:var(--muted);transition:transform .2s ease;}
  summary .chev::before{left:0;transform:rotate(45deg);}
  summary .chev::after{right:0;transform:rotate(-45deg);}
  details[open]>summary .chev::before{transform:rotate(-45deg);}
  details[open]>summary .chev::after{transform:rotate(45deg);}
  summary:hover .cat-name{color:var(--accent);}
  .catbody{border-top:1px solid var(--line);}
  .curnote{color:var(--muted);font-size:.73rem;margin:8px 12px;line-height:1.5;}
  .grp-label{color:var(--accent-b);font-size:.76rem;font-weight:700;
    margin:10px 12px 4px;letter-spacing:.3px;text-transform:uppercase;}
  .err{color:var(--warn);font-size:.82rem;margin:10px 12px;}
  .noresult{color:var(--muted);font-size:.82rem;margin:12px;text-align:center;}

  /* 搜尋 */
  .searchbar{display:flex;align-items:center;gap:7px;margin:10px 12px 4px;
    border:1px solid var(--line2);border-radius:10px;padding:6px 10px;background:var(--card2);
    transition:border-color .18s,box-shadow .18s;}
  .searchbar:focus-within{border-color:var(--accent-b);box-shadow:0 0 0 3px rgba(78,161,255,.16);}
  .searchbar .sicon{width:15px;height:15px;flex:0 0 15px;fill:none;stroke:var(--muted);stroke-width:2;stroke-linecap:round;}
  .searchbar input{flex:1;min-width:0;border:0;background:transparent;color:var(--fg);font-size:.88rem;outline:none;}
  .searchbar input::placeholder{color:var(--muted);}
  .sclear{border:0;background:transparent;color:var(--muted);font-size:1.1rem;line-height:1;cursor:pointer;padding:0 3px;visibility:hidden;}
  .searchbar.has-q .sclear{visibility:visible;}
  /* 國外全域搜尋列：分頁頂端 */
  .fsearch{margin:0 0 12px;position:sticky;top:0;z-index:5;}
  .fsearch-hint{color:var(--accent-b);font-size:.75rem;margin:0 2px 12px;font-weight:600;}

  /* 表格 */
  .tw{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;}
  table{width:100%;border-collapse:collapse;}
  th,td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top;}
  thead th{position:sticky;top:0;background:var(--th);font-size:.7rem;
    color:var(--muted);font-weight:600;white-space:nowrap;z-index:1;letter-spacing:.2px;}
  th.l,td.l{text-align:left;}
  th.r,td.r{text-align:right;white-space:nowrap;}
  td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;}
  tbody tr:nth-child(even){background:var(--zebra);}
  tbody tr.mrow{transition:background .12s;}
  tbody tr.mrow:hover{background:rgba(78,161,255,.07);}
  td.lots{color:var(--accent);font-weight:700;text-shadow:0 0 10px rgba(53,224,214,.25);}
  td.lots.zero{color:var(--warn);text-shadow:none;}
  td.vol{color:var(--muted);}
  .cur{display:inline-block;margin-left:5px;padding:0 5px;border-radius:5px;
    background:rgba(255,180,84,.16);color:var(--warn);font-size:.65rem;font-weight:700;font-family:var(--mono);}
  .kind{display:inline-block;margin-left:5px;padding:0 5px;border-radius:5px;
    background:rgba(167,139,250,.18);color:var(--accent-v);font-size:.63rem;font-weight:700;}
  .ucode{display:block;color:var(--muted);font-size:.68rem;margin-top:1px;font-family:var(--mono);}
  /* 股票表緊湊 */
  table.stock th,table.stock td{padding:6px 5px;}
  table.stock td.l{min-width:100px;max-width:155px;}
  table.stock th.r,table.stock td.r{padding-left:2px;padding-right:4px;}
  @media(max-width:520px){
    table.stock th,table.stock td{padding:5px 2px;}
    table.stock th.r,table.stock td.r{padding-left:1px;padding-right:2px;}
  }
  /* 國外幣別徽章 */
  .cur-badge{display:inline-block;padding:1px 7px;border-radius:6px;font-size:.68rem;font-weight:700;font-family:var(--mono);}
  .cur-usd{background:rgba(78,161,255,.16);color:var(--accent-b);}
  .cur-jpy{background:rgba(255,180,84,.14);color:var(--warn);}
  .cur-hkd{background:rgba(255,100,100,.14);color:#ff8080;}
  .cur-sgd{background:rgba(53,224,214,.14);color:var(--accent);}
  .cur-eur{background:rgba(167,139,250,.16);color:var(--accent-v);}
  .cur-gbp{background:rgba(200,180,100,.14);color:#d4b96a;}

  /* ---- 浮動快捷按鈕 ---- */
  .fab-wrap{position:fixed;bottom:24px;right:16px;z-index:100;display:flex;flex-direction:column;align-items:flex-end;gap:8px;}
  .fab-menu{display:none;flex-direction:column;align-items:flex-end;gap:6px;margin-bottom:4px;
    max-height:70vh;overflow-y:auto;padding:2px;}
  .fab-menu.open{display:flex;}
  .fab-section-label{font-size:.65rem;color:var(--accent);text-align:right;padding:2px 4px 2px;
    letter-spacing:.4px;width:100%;font-weight:700;text-transform:uppercase;}
  .fab-grid{display:flex;flex-direction:column;gap:5px;width:100%;}
  .fab-item{border:1px solid var(--accent-b);background:rgba(14,22,38,.92);color:var(--fg);
    border-radius:10px;padding:7px 14px;font-size:.8rem;font-weight:600;cursor:pointer;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:background .15s,border-color .15s,color .15s;
    box-shadow:0 4px 16px rgba(0,0,0,.45);text-align:left;backdrop-filter:blur(8px);}
  .fab-item:hover{background:rgba(78,161,255,.25);border-color:var(--accent);color:var(--accent);}
  @media(prefers-color-scheme:light){
    .fab-item{background:rgba(255,255,255,.96);color:var(--fg);border-color:var(--accent-b);}
    .fab-item:hover{background:rgba(47,127,240,.12);}
  }
  @keyframes fab-pulse{
    0%,100%{box-shadow:0 4px 20px rgba(53,224,214,.35),0 0 0 0 rgba(53,224,214,0);}
    50%{box-shadow:0 4px 20px rgba(53,224,214,.5),0 0 0 8px rgba(53,224,214,.12);}
  }
  .fab-btn{width:37px;height:37px;border-radius:50%;border:0;cursor:pointer;
    background:linear-gradient(135deg,var(--accent),var(--accent-b));
    color:#0a0e17;font-size:1.05rem;font-weight:900;
    box-shadow:0 4px 20px rgba(53,224,214,.35);
    display:flex;align-items:center;justify-content:center;
    transition:transform .15s,box-shadow .15s;
    animation:fab-pulse 2.5s ease-in-out infinite;}
  .fab-btn:hover{transform:scale(1.1);box-shadow:0 6px 28px rgba(53,224,214,.6);animation:none;}
  .fab-btn:active{transform:scale(.92);}

  footer{color:var(--muted);font-size:.7rem;text-align:center;padding:16px 8px 0;line-height:1.75;}
  footer a{color:var(--accent-b);}
</style>
</head>
<body>
<div class="wrap">
  <header class="bar">
    <div class="bar-top">
      <h1>
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#35e0d6"/><stop offset="0.55" stop-color="#4ea1ff"/>
            <stop offset="1" stop-color="#a78bfa"/></linearGradient></defs>
          <rect x="2.5" y="2.5" width="19" height="19" rx="5" stroke="url(#lg)" stroke-width="1.7"/>
          <path d="M6 15l3.5-4 3 2.4L18 8" stroke="url(#lg)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="ttl">華南期貨商品保證金一覽</span>
      </h1>
      <!-- 字級按鈕：固定在 header 右側，兩個 tab 都可用 -->
      <div class="fsgroup" role="group" aria-label="字級調整" style="margin-left:auto;">
        <button class="fsbtn" id="fsDown" aria-label="縮小字級">A−</button>
        <button class="fsbtn mid" id="fsReset" aria-label="預設字級">A</button>
        <button class="fsbtn" id="fsUp" aria-label="放大字級">A＋</button>
      </div>
      <nav class="social" aria-label="社群">
        <a href="https://www.instagram.com/f1_futures/" target="_blank" rel="noopener noreferrer" aria-label="Instagram">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="2" width="20" height="20" rx="5.5"/>
            <circle cx="12" cy="12" r="4.3"/>
            <circle cx="17.6" cy="6.4" r="0.9" fill="currentColor" stroke="none"/>
          </svg>
        </a>
        <a href="https://www.threads.com/@f1_futures" target="_blank" rel="noopener noreferrer" aria-label="Threads">
          <svg viewBox="0 0 192 192" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
            <path d="M141.537 88.988a66.667 66.667 0 0 0-2.518-1.143c-1.482-27.307-16.403-42.94-41.457-43.1h-.34c-14.986 0-27.449 6.396-35.12 18.036l13.779 9.452c5.73-8.695 14.724-10.548 21.348-10.548h.229c8.249.053 14.474 2.452 18.503 7.129 2.932 3.405 4.893 8.111 5.864 14.05-7.314-1.243-15.224-1.626-23.68-1.14-23.82 1.371-39.134 15.264-38.105 34.568.522 9.792 5.4 18.216 13.735 23.719 7.047 4.652 16.124 6.927 25.557 6.412 12.458-.683 22.231-5.436 29.05-14.127 5.177-6.6 8.452-15.153 9.898-25.93 5.937 3.583 10.337 8.298 12.767 13.966 4.132 9.635 4.373 25.468-8.546 38.376-11.319 11.308-24.925 16.2-45.488 16.35-22.809-.169-40.06-7.484-51.275-21.742C35.236 139.966 29.808 120.682 29.605 96c.203-24.682 5.63-43.966 16.133-57.317C56.954 24.425 74.204 17.11 97.013 16.94c22.975.17 40.526 7.52 52.171 21.847 5.71 7.026 10.015 15.86 12.853 26.162l16.147-4.308c-3.44-12.68-8.853-23.606-16.219-32.668C147.036 10.646 125.202 1.205 97.07 1 68.954 1.205 47.39 10.68 32.864 28.062 19.772 43.812 13.04 66.05 12.807 96c.233 29.95 6.965 52.19 20.057 67.94C47.39 181.32 68.954 190.795 97.07 191c25.317-.176 43.035-6.803 57.708-21.466 19.198-19.187 18.616-43.27 12.285-58.052-4.557-10.622-13.183-19.283-25.526-24.494ZM96.597 144.024c-10.426.58-21.24-4.1-26.896-11.768-3.612-4.83-3.744-10.68-.37-14.751 4.36-5.24 13.491-8.066 26.12-8.794 2.28-.132 4.512-.193 6.694-.193 6.048 0 11.72.558 16.894 1.628-1.917 23.658-11.386 32.503-22.442 33.878Z"/>
          </svg>
        </a>
      </nav>
    </div>
    <div class="tabs" role="tablist">
      <button class="tab-btn active" role="tab" aria-selected="true"  data-tab="domestic">🇹🇼 國內期貨</button>
      <button class="tab-btn"        role="tab" aria-selected="false" data-tab="foreign" >🌐 國外期貨</button>
    </div>
    <div id="domestic-controls" class="controls">
      <div class="calc">
        <label for="avail">可用保證金</label>
        <button class="stepper" id="stepDown" aria-label="減少十萬">−</button>
        <input id="avail" inputmode="numeric" autocomplete="off" placeholder="輸入金額" value="3,000,000" />
        <button class="stepper" id="stepUp" aria-label="增加十萬">＋</button>
        <span class="ccy">NT$</span>
      </div>
    </div>
    <div class="hint" id="domestic-hint">輸入「可用保證金」後，下方各表最右欄即時換算「可下單口數」（無條件捨去）。外幣計價商品需自行換匯，不計入口數。每次加減 10 萬元。</div>
  </header>

  <!-- 國內期貨 -->
  <div class="tab-panel active" id="panel-domestic">
    {{DOMESTIC_SECTIONS}}
  </div>

  <!-- 國外期貨 -->
  <div class="tab-panel" id="panel-foreign">
    <div class="searchbar fsearch">
      <svg class="sicon" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="11" cy="11" r="7"></circle>
        <line x1="21" y1="21" x2="16.5" y2="16.5"></line></svg>
      <input id="foreignSearch" type="search" inputmode="search" autocomplete="off"
             placeholder="搜尋商品名稱／代號，或輸入數字篩選保證金上限（例：黃金、JAU、50000）" />
      <button type="button" id="foreignClear" class="sclear" aria-label="清除搜尋">×</button>
    </div>
    <p class="fsearch-hint" id="foreignSearchHint" hidden></p>
    {{FOREIGN_SECTIONS}}
  </div>

  <footer>
    資料來源：台灣期貨交易所與華南期貨官方網站。<br>
    本網頁於每日早上與晚上各更新 1 次。<br>
<<<<<<< HEAD
    本資料僅供參考，實際保證金以各交易所及期貨商公告為準。
=======
    本資料僅供參考，實際保證金以各交易所及期貨商公告為準。<br>
    <span class="vcounter" id="vcounter" title="累計瀏覽次數">👁 ...</span>
>>>>>>> ed1e1a8e3e1e4b5982a3fa59d04b952251caba7b
  </footer>
</div>

<!-- 浮動快捷按鈕 -->
<div class="fab-wrap">
  <div class="fab-menu" id="fabMenu">
    <div id="fabDomesticNav">
      <div class="fab-section-label">國內期貨</div>
      {{DOMESTIC_NAV}}
    </div>
    <div id="fabForeignNav" style="display:none">
      <div class="fab-section-label">國外期貨</div>
      {{FOREIGN_NAV}}
    </div>
  </div>
  <button class="fab-btn" id="fabBtn" aria-label="快速跳轉">☰</button>
</div>

<script>
(function(){
  /* ---- 反爬 ---- */
  document.addEventListener('contextmenu',function(e){e.preventDefault();});
  document.addEventListener('keydown',function(e){
    if(e.key==='F12'||(e.ctrlKey&&e.shiftKey&&(e.key==='I'||e.key==='J'||e.key==='C'))||
       (e.ctrlKey&&e.key==='U')){e.preventDefault();}
  });
  document.addEventListener('selectstart',function(e){
    if(e.target.tagName!=='INPUT'&&e.target.tagName!=='TEXTAREA'){e.preventDefault();}
  });

  var root=document.documentElement;

  /* ---- 字級 ---- */
  var sizes=[11,12,13,14,15,16,18,20],idx=5; // 預設 16px
  function applyFs(){root.style.setProperty('--fs',sizes[idx]+'px');}
  document.getElementById('fsUp').addEventListener('click',function(){if(idx<sizes.length-1){idx++;applyFs();}});
  document.getElementById('fsDown').addEventListener('click',function(){if(idx>0){idx--;applyFs();}});
  document.getElementById('fsReset').addEventListener('click',function(){idx=5;applyFs();});
<<<<<<< HEAD
=======

  /* ---- 訪客計數器（countapi.xyz） ---- */
  (function(){
    var el=document.getElementById('vcounter');
    if(!el)return;
    fetch('https://api.countapi.xyz/hit/hahabao-tw.github.io/tw-margin-table')
      .then(function(r){return r.json();})
      .then(function(d){if(d&&d.value)el.textContent='👁 '+d.value.toLocaleString();})
      .catch(function(){el.style.display='none';});
  })();
>>>>>>> ed1e1a8e3e1e4b5982a3fa59d04b952251caba7b

  /* ---- 分頁切換 ---- */
  var tabs=Array.prototype.slice.call(document.querySelectorAll('.tab-btn'));
  var panels={domestic:document.getElementById('panel-domestic'),foreign:document.getElementById('panel-foreign')};
  var domCtrl=document.getElementById('domestic-controls');
  var domHint=document.getElementById('domestic-hint');
  var fabDomNav=document.getElementById('fabDomesticNav');
  var fabForNav=document.getElementById('fabForeignNav');
  var currentTab='domestic';
  function switchTab(t){
    currentTab=t;
    tabs.forEach(function(b){
      var on=b.getAttribute('data-tab')===t;
      b.classList.toggle('active',on);
      b.setAttribute('aria-selected',on?'true':'false');
    });
    panels.domestic.classList.toggle('active',t==='domestic');
    panels.foreign.classList.toggle('active',t==='foreign');
    domCtrl.style.display=t==='domestic'?'':'none';
    domHint.style.display=t==='domestic'?'':'none';
    fabDomNav.style.display=t==='domestic'?'':'none';
    fabForNav.style.display=t==='foreign'?'':'none';
    closeFab();
  }
  tabs.forEach(function(b){
    b.addEventListener('click',function(){switchTab(b.getAttribute('data-tab'));});
  });

  /* ---- 可用保證金 ---- */
  var input=document.getElementById('avail');
  var rows=Array.prototype.slice.call(document.querySelectorAll('tr.mrow'));
  function parseAmount(v){
    if(!v)return null;
    v=String(v).replace(/[^0-9.]/g,'');
    if(v==='')return null;
    var n=parseFloat(v);
    return(isFinite(n)&&n>0)?n:null;
  }
  function setAmount(n){
    if(n<0)n=0;
    input.value=n.toLocaleString('en-US');
    update();
  }
  function update(){
    var avail=parseAmount(input.value);
    for(var i=0;i<rows.length;i++){
      var tr=rows[i];
      var cell=tr.querySelector('.lots');
      if(!cell)continue;
      var ccy=tr.getAttribute('data-currency')||'TWD';
      var m=tr.getAttribute('data-margin');
      cell.classList.remove('zero');
      if(avail===null){cell.textContent='—';continue;}
      if(ccy!=='TWD'){cell.textContent='—';continue;}
      if(m===''||m===null){cell.textContent='—';continue;}
      var margin=parseFloat(m);
      if(!(margin>0)){cell.textContent='—';continue;}
      var lots=Math.floor(avail/margin);
      cell.textContent=lots.toLocaleString();
      if(lots===0){cell.classList.add('zero');}
    }
  }
  input.addEventListener('input',function(){
    var n=parseAmount(input.value);
    if(n!==null){input.value=n.toLocaleString('en-US');}
    update();
  });
  document.getElementById('stepUp').addEventListener('click',function(){setAmount((parseAmount(input.value)||0)+100000);});
  document.getElementById('stepDown').addEventListener('click',function(){setAmount(Math.max(0,(parseAmount(input.value)||0)-100000));});
  update();

  /* ---- 搜尋（支援多個搜尋框）---- */
  function initSearch(searchId,clearId,noresultId,rowSel){
    var s=document.getElementById(searchId);
    if(!s)return;
    var c=document.getElementById(clearId);
    var nr=document.getElementById(noresultId);
    var bar=s.closest('.searchbar');
    var det=s.closest('details.cat');
    var srows=Array.prototype.slice.call(document.querySelectorAll(rowSel));
    function norm(x){return(x||'').toLowerCase().trim();}
    function run(){
      var q=norm(s.value);
      bar.classList.toggle('has-q',q.length>0);
      if(det&&q.length>0&&!det.open){det.open=true;}
      var shown=0;
      for(var i=0;i<srows.length;i++){
        var key=srows[i].getAttribute('data-search')||'';
        var hit=(q===''||key.indexOf(q)!==-1);
        srows[i].style.display=hit?'':'none';
        if(hit)shown++;
      }
      if(nr){nr.hidden=!(q!==''&&shown===0);}
    }
    s.addEventListener('input',run);
    if(c){c.addEventListener('click',function(){s.value='';run();s.focus();});}
  }
  initSearch('stockSearch','stockClear','stockNoResult','#cat-stock tr.srow');
  initSearch('etfSearch','etfClear','etfNoResult','#cat-etf tr.srow');

  /* ---- 國外全域搜尋（文字搜商品 / 數字搜保證金上限）---- */
  (function(){
    var fs=document.getElementById('foreignSearch');
    if(!fs)return;
    var clr=document.getElementById('foreignClear');
    var bar=fs.closest('.searchbar');
    var hint=document.getElementById('foreignSearchHint');
    var frows=Array.prototype.slice.call(document.querySelectorAll('tr.frow'));
    var fdetails=Array.prototype.slice.call(document.querySelectorAll('#panel-foreign details.cat'));
    function run(){
      var q=(fs.value||'').trim();
      bar.classList.toggle('has-q',q.length>0);
      // 判斷是否為純數字（可含逗號）
      var numQ=q.replace(/,/g,'');
      var isNum=q!==''&&/^[0-9]+$/.test(numQ);
      var threshold=isNum?parseInt(numQ,10):null;
      var lq=q.toLowerCase();
      var shown=0;
      for(var i=0;i<frows.length;i++){
        var tr=frows[i];
        var hit;
        if(q===''){
          hit=true;
        }else if(isNum){
          var m=parseFloat(tr.getAttribute('data-margin'));
          hit=(m<=threshold);   // 列出比輸入數字「少」的保證金
        }else{
          var key=tr.getAttribute('data-search')||'';
          hit=key.indexOf(lq)!==-1;
        }
        tr.style.display=hit?'':'none';
        if(hit)shown++;
      }
      // 有搜尋時自動展開所有國外交易所，方便看到結果
      if(q!==''){fdetails.forEach(function(d){if(!d.open)d.open=true;});}
      // 隱藏沒有可見列的「分類表格 + 其標題」
      var tables=document.querySelectorAll('#panel-foreign table');
      for(var t=0;t<tables.length;t++){
        var tbl=tables[t];
        var vis=tbl.querySelectorAll('tr.frow:not([style*="display: none"])').length;
        var wrap=tbl.closest('.tw');
        var label=wrap?wrap.previousElementSibling:null;
        var hideGrp=(q!==''&&vis===0);
        if(wrap)wrap.style.display=hideGrp?'none':'';
        if(label&&label.classList.contains('grp-label'))label.style.display=hideGrp?'none':'';
      }
      // 隱藏完全沒有結果的交易所區塊
      fdetails.forEach(function(d){
        var visible=d.querySelectorAll('tr.frow:not([style*="display: none"])').length;
        d.style.display=(q!==''&&visible===0)?'none':'';
      });
      // 提示文字
      if(hint){
        if(isNum){
          hint.hidden=false;
          hint.textContent='顯示原始保證金 ≤ '+threshold.toLocaleString()+' 的商品，共 '+shown+' 項';
        }else if(q!==''){
          hint.hidden=false;
          hint.textContent='找到 '+shown+' 項商品';
        }else{
          hint.hidden=true;
        }
      }
    }
    fs.addEventListener('input',run);
    if(clr){clr.addEventListener('click',function(){fs.value='';run();fs.focus();});}
  })();
  /* ---- 浮動快捷按鈕 ---- */
  var fabBtn=document.getElementById('fabBtn');
  var fabMenu=document.getElementById('fabMenu');
  var fabOpen=false;
  function closeFab(){fabOpen=false;fabMenu.classList.remove('open');fabBtn.textContent='☰';}
  fabBtn.addEventListener('click',function(e){
    e.stopPropagation();
    fabOpen=!fabOpen;
    fabMenu.classList.toggle('open',fabOpen);
    fabBtn.textContent=fabOpen?'✕':'☰';
  });
  document.addEventListener('click',function(){closeFab();});
  fabMenu.addEventListener('click',function(e){e.stopPropagation();});
  document.querySelectorAll('.fab-item').forEach(function(btn){
    btn.addEventListener('click',function(){
      var tid=btn.getAttribute('data-target');
      var el=document.getElementById(tid);
      if(el){
        if(el.tagName==='DETAILS'&&!el.open){el.open=true;}
        setTimeout(function(){el.scrollIntoView({behavior:'smooth',block:'start'});},50);
      }
      closeFab();
    });
  });
})();
</script>
</body>
</html>
"""

def main():
    ds = build_dataset()
    html = render_html(ds)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    # 同時輸出一份 JSON 備份（除錯/未來擴充用）
    try:
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(ds, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    print("已產生 index.html", file=sys.stderr)


if __name__ == "__main__":
    main()
