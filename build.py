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
        n1, n2, n3 = _clean_num(fields[1]), _clean_num(fields[2]), _clean_num(fields[3])
        # 一筆有效資料：商品名 + 結算/維持/原始 三個數字
        if n1 is None or n2 is None or n3 is None:
            continue
        rows.append({"name": name, "original_margin": n3})
    return rows


def parse_stock_margin(text):
    """
    解析『股票類』CSV（含 (一)股票 / (二)ETF 兩段）。
    回傳 [{code, ucode, name, ratio, kind}]
      kind: 'stock' | 'mini' | 'etf'
      ratio: 原始保證金適用比例 (0~1)
    """
    rows = []
    section = "stock"  # 預設標的為股票
    for line in text.splitlines():
        if not line.strip():
            continue
        # 區段標題判斷
        if "標的證券為" in line or "ＥＴＦ" in line or "ETF" in line:
            if "ETF" in line or "ＥＴＦ" in line:
                section = "etf"
            elif "股票" in line:
                section = "stock"
            # 其他（如受益憑證）也歸 ETF 類處理（乘數 10000）
            elif "受益" in line:
                section = "etf"
            continue
        fields = _split_csv_line(line)
        if len(fields) < 9:
            continue
        # 第一欄需為序號（整數）
        seq = fields[0].strip()
        if not seq.isdigit():
            continue
        code = fields[1].strip()
        ucode = fields[2].strip()
        name = fields[3].strip()
        ratio = _clean_num(fields[8])  # 原始保證金適用比例
        if ratio is None or not code:
            continue
        if section == "etf":
            kind = "etf"
        elif name.startswith("小型"):
            kind = "mini"
        else:
            kind = "stock"
        rows.append({"code": code, "ucode": ucode, "name": name,
                     "ratio": ratio, "kind": kind})
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


def _get(rec, keys):
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


def get_close_prices():
    """
    回傳 {contract_code: price, product_name: price}。
    取一般交易時段、各商品近月（最早到期月份）有效成交價，
    無最後成交價時退而求其次用結算價。
    """
    try:
        raw = fetch_bytes(DAILY_REPORT_URL,
                          headers={"Accept": "application/json"})
        data = json.loads(decode_text(raw))
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 每日行情抓取/解析失敗：{e}", file=sys.stderr)
        return {}

    if not isinstance(data, list) or not data:
        print("  [warn] 每日行情回傳格式非預期（非陣列或空）", file=sys.stderr)
        return {}

    # 偵錯：列出第一筆的欄位，方便日後若欄位改名時對照
    print(f"  每日行情筆數：{len(data)}；首筆欄位：{list(data[0].keys())}",
          file=sys.stderr)

    # 蒐集每個契約的（到期月份, 價格, 名稱）
    best = {}  # code -> (month_str, price)
    name_best = {}  # name -> (month_str, price)
    kept = 0
    for rec in data:
        session = _get(rec, KEY_SESSION)
        # 僅取一般交易時段；若無此欄位則全收（多數 OpenAPI 即為一般時段）
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
        kept += 1
        # 近月 = 到期月份字串最小者
        if code not in best or month < best[code][0]:
            best[code] = (month, price)
        name = _get(rec, KEY_NAME)
        if name:
            name = str(name).strip()
            if name not in name_best or month < name_best[name][0]:
                name_best[name] = (month, price)

    price_map = {c: v[1] for c, v in best.items()}
    for n, v in name_best.items():
        price_map.setdefault(n, v[1])
    print(f"  收盤價可用商品數：{len(best)}（有效列 {kept}）", file=sys.stderr)
    return price_map


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
    sections = []   # 每個交易所分類一段
    update_dates = {}

    price_map = {}
    # 先抓收盤價（股票期貨需要）
    print("抓取每日行情（收盤價）…", file=sys.stderr)
    price_map = get_close_prices()

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
            rows = []
            matched = 0
            for r in parsed:
                mult = {"stock": MULT_STOCK, "mini": MULT_MINI,
                        "etf": MULT_ETF}[r["kind"]]
                # 以英文代碼對接收盤價；退而求其次用中文簡稱
                price = price_map.get(r["code"])
                if price is None:
                    price = price_map.get(r["name"])
                if price is not None:
                    matched += 1
                    margin = round(price * mult * r["ratio"])
                else:
                    margin = None
                rows.append({
                    "code": r["code"],
                    "ucode": r["ucode"],
                    "name": r["name"],
                    "ratio": r["ratio"],
                    "kind": r["kind"],
                    "mult": mult,
                    "price": price,
                    "margin": margin,
                    "currency": "TWD",
                })
            print(f"  股票期貨 {len(rows)} 檔，成功對到收盤價 {matched} 檔",
                  file=sys.stderr)
            sections.append({"key": src["key"], "title": src["title"],
                             "kind": "stock", "rows": rows,
                             "update": update_dates[src["key"]],
                             "matched": matched})

    return {
        "sections": sections,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC",
        "generated_tw": (datetime.datetime.utcnow() +
                         datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
    }


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


def render_simple_section(sec):
    parts = [f'<section class="cat" data-cat="{esc(sec["key"])}">']
    parts.append(f'<h2>{esc(sec["title"])}'
                 f'<span class="upd">更新：{esc(sec.get("update") or "—")}</span></h2>')
    if sec.get("error"):
        parts.append(f'<p class="err">資料暫時無法取得：{esc(sec["error"])}</p></section>')
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
    parts.append('</tbody></table></div></section>')
    return "".join(parts)


def render_stock_section(sec):
    parts = [f'<section class="cat" data-cat="stock">']
    matched = sec.get("matched", 0)
    total = len(sec["rows"])
    parts.append(f'<h2>{esc(sec["title"])}'
                 f'<span class="upd">更新：{esc(sec.get("update") or "—")}</span></h2>')
    if sec.get("error"):
        parts.append(f'<p class="err">資料暫時無法取得：{esc(sec["error"])}</p></section>')
        return "".join(parts)
    parts.append('<p class="curnote">單位：NT$（TWD）；'
                 '原始保證金 = 收盤價 × 契約乘數 × 原始保證金適用比例。'
                 f'<br>已對到收盤價 {matched}/{total} 檔（無收盤價者以「—」表示）。</p>')
    parts.append('<div class="tw"><table class="stock">')
    parts.append('<thead><tr>'
                 '<th class="l">商品（標的）</th>'
                 '<th class="r">適用比例</th>'
                 '<th class="r">收盤價</th>'
                 '<th class="r">原始保證金</th>'
                 '<th class="r">可下單口數</th>'
                 '</tr></thead><tbody>')
    kind_label = {"stock": "", "mini": "小", "etf": "ETF"}
    for r in sec["rows"]:
        tag = kind_label.get(r["kind"], "")
        tagspan = f'<span class="kind">{esc(tag)}</span>' if tag else ""
        sub = f'<span class="ucode">{esc(r["ucode"])}・×{r["mult"]:,}</span>'
        parts.append(
            '<tr class="mrow" '
            f'data-margin="{r["margin"] if r["margin"] is not None else ""}" '
            'data-currency="TWD">'
            f'<td class="l">{esc(r["name"])}{tagspan}<br>{sub}</td>'
            f'<td class="r num">{fmt_pct(r["ratio"])}</td>'
            f'<td class="r num">{fmt_price(r["price"])}</td>'
            f'<td class="r num">{fmt_int(r["margin"])}</td>'
            f'<td class="r num lots">—</td>'
            '</tr>')
    parts.append('</tbody></table></div></section>')
    return "".join(parts)


def render_html(ds):
    body = []
    for sec in ds["sections"]:
        if sec["kind"] == "stock":
            body.append(render_stock_section(sec))
        else:
            body.append(render_simple_section(sec))
    sections_html = "\n".join(body)

    return HTML_TEMPLATE.replace("{{SECTIONS}}", sections_html) \
                        .replace("{{GEN_TW}}", esc(ds["generated_tw"])) \
                        .replace("{{GEN_UTC}}", esc(ds["generated_at"]))


# HTML 模板（CSS / JS 內嵌，單檔即可放上 GitHub Pages）
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>期貨商保證金一覽表</title>
<style>
  :root{
    --fs: 16px;
    --bg:#0f1217; --card:#161b22; --line:#2a313c; --fg:#e6edf3;
    --muted:#8b97a6; --accent:#4ea1ff; --accent2:#2d8c5f; --warn:#e0883a;
    --th:#1c232d; --zebra:#12161c;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f3f5f8; --card:#ffffff; --line:#dde3ea; --fg:#1b2330;
           --muted:#5b6675; --th:#eef2f7; --zebra:#f7f9fc; }
  }
  *{ box-sizing:border-box; }
  html,body{ margin:0; padding:0; background:var(--bg); color:var(--fg);
    font-family:-apple-system,"Segoe UI",Roboto,"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;
    font-size:var(--fs); line-height:1.5; -webkit-text-size-adjust:100%; }
  .wrap{ max-width:820px; margin:0 auto; padding:0 12px 64px; }

  header.bar{ position:sticky; top:0; z-index:20; background:var(--card);
    border-bottom:1px solid var(--line); padding:10px 12px;
    margin:0 -12px 14px; backdrop-filter:saturate(1.2) blur(2px); }
  .bar h1{ font-size:1.05rem; margin:0 0 8px; display:flex; align-items:center; gap:8px; }
  .bar h1 .dot{ width:9px;height:9px;border-radius:50%;background:var(--accent2); }
  .controls{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .fsgroup{ display:flex; gap:4px; }
  .fsbtn{ border:1px solid var(--line); background:transparent; color:var(--fg);
    border-radius:8px; padding:6px 10px; font-size:0.95rem; cursor:pointer; min-width:38px; }
  .fsbtn:active{ transform:scale(.96); }
  .fsbtn.mid{ font-weight:600; }
  .calc{ flex:1 1 200px; display:flex; align-items:center; gap:6px;
    border:1px solid var(--line); border-radius:10px; padding:4px 8px; background:var(--bg); }
  .calc label{ color:var(--muted); font-size:0.82rem; white-space:nowrap; }
  .calc input{ flex:1; min-width:0; border:0; background:transparent; color:var(--fg);
    font-size:1.05rem; font-variant-numeric:tabular-nums; outline:none; text-align:right; }
  .calc .ccy{ color:var(--muted); font-size:0.85rem; }
  .hint{ color:var(--muted); font-size:0.78rem; margin:6px 2px 0; }

  section.cat{ background:var(--card); border:1px solid var(--line);
    border-radius:14px; margin:0 0 16px; overflow:hidden; }
  section.cat h2{ font-size:1rem; margin:0; padding:12px 14px 10px;
    border-bottom:1px solid var(--line); display:flex; align-items:baseline;
    justify-content:space-between; gap:8px; position:sticky; top:0; }
  section.cat h2 .upd{ color:var(--muted); font-size:0.74rem; font-weight:400; white-space:nowrap; }
  .curnote{ color:var(--muted); font-size:0.8rem; margin:8px 14px; }
  .err{ color:var(--warn); font-size:0.86rem; margin:12px 14px; }

  .tw{ width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  table{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th,td{ padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  thead th{ position:sticky; top:0; background:var(--th); font-size:0.8rem;
    color:var(--muted); font-weight:600; white-space:nowrap; z-index:1; }
  th.l,td.l{ text-align:left; }
  th.r,td.r{ text-align:right; white-space:nowrap; }
  td.num{ font-variant-numeric:tabular-nums; }
  tbody tr:nth-child(even){ background:var(--zebra); }
  td.lots{ color:var(--accent); font-weight:600; }
  td.lots.zero{ color:var(--warn); font-weight:600; }
  .cur{ display:inline-block; margin-left:6px; padding:1px 6px; border-radius:6px;
    background:rgba(224,136,58,.16); color:var(--warn); font-size:0.7rem; font-weight:600; }
  .kind{ display:inline-block; margin-left:6px; padding:0 6px; border-radius:6px;
    background:rgba(78,161,255,.16); color:var(--accent); font-size:0.7rem; font-weight:600; }
  .ucode{ display:block; color:var(--muted); font-size:0.74rem; margin-top:2px; }

  footer{ color:var(--muted); font-size:0.78rem; text-align:center; padding:18px 8px 0; line-height:1.7; }
  footer a{ color:var(--accent); }
  /* 股票表第一欄較寬、數字欄緊湊 */
  table.stock td.l{ min-width:128px; }
  table.stock th.r, table.stock td.r{ padding-left:6px; padding-right:8px; }
</style>
</head>
<body>
<div class="wrap">
  <header class="bar">
    <h1><span class="dot"></span>期貨商保證金一覽表</h1>
    <div class="controls">
      <div class="fsgroup" role="group" aria-label="字級調整">
        <button class="fsbtn" id="fsDown" aria-label="縮小字級">A−</button>
        <button class="fsbtn mid" id="fsReset" aria-label="預設字級">A</button>
        <button class="fsbtn" id="fsUp" aria-label="放大字級">A＋</button>
      </div>
      <div class="calc">
        <label for="avail">可用保證金</label>
        <input id="avail" inputmode="numeric" autocomplete="off"
               placeholder="輸入金額" />
        <span class="ccy">NT$</span>
      </div>
    </div>
    <div class="hint">輸入「可用保證金」後，下方各表最右欄即時換算「可下單口數」（無條件捨去）。外幣計價商品需自行換匯，不計入口數。</div>
  </header>

  {{SECTIONS}}

  <footer>
    資料來源：臺灣期貨交易所（TAIFEX）原始保證金一覽表與每日交易行情（一般交易時段）。<br>
    本頁每日自動更新；資料僅供參考，實際保證金以期交所及各期貨商公告為準。<br>
    產生時間：{{GEN_TW}}（台北）／{{GEN_UTC}}
  </footer>
</div>

<script>
(function(){
  // ---- 字級調整（記憶於 localStorage 之外，採 inline，避免儲存限制）----
  var root = document.documentElement;
  var sizes = [13,14,15,16,17,18,20,22];
  var idx = 3; // 預設 16px
  function applyFs(){ root.style.setProperty('--fs', sizes[idx] + 'px'); }
  document.getElementById('fsUp').addEventListener('click', function(){
    if(idx < sizes.length-1){ idx++; applyFs(); }
  });
  document.getElementById('fsDown').addEventListener('click', function(){
    if(idx > 0){ idx--; applyFs(); }
  });
  document.getElementById('fsReset').addEventListener('click', function(){
    idx = 3; applyFs();
  });

  // ---- 可下單口數即時換算 ----
  var input = document.getElementById('avail');
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr.mrow'));

  function parseAmount(v){
    if(!v) return null;
    v = String(v).replace(/[^0-9.]/g,'');
    if(v === '') return null;
    var n = parseFloat(v);
    return (isFinite(n) && n > 0) ? n : null;
  }
  function update(){
    var avail = parseAmount(input.value);
    for(var i=0;i<rows.length;i++){
      var tr = rows[i];
      var cell = tr.querySelector('.lots');
      if(!cell) continue;
      var ccy = tr.getAttribute('data-currency') || 'TWD';
      var m = tr.getAttribute('data-margin');
      cell.classList.remove('zero');
      if(avail === null){ cell.textContent = '—'; continue; }
      if(ccy !== 'TWD'){ cell.textContent = '—'; continue; } // 外幣不換算
      if(m === '' || m === null){ cell.textContent = '—'; continue; }
      var margin = parseFloat(m);
      if(!(margin > 0)){ cell.textContent = '—'; continue; }
      var lots = Math.floor(avail / margin);
      cell.textContent = lots.toLocaleString();
      if(lots === 0){ cell.classList.add('zero'); }
    }
  }
  // 千分位顯示輸入值
  input.addEventListener('input', function(){
    var caretEnd = (input.selectionStart === input.value.length);
    var n = parseAmount(input.value);
    if(n !== null){
      var s = n.toLocaleString('en-US');
      input.value = s;
    }
    update();
  });
  update();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
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
