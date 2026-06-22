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
    解析『股票類』CSV（含 (一)股票期貨 / (二)ETF期貨 兩段）。
    遇到「選擇權契約」段落即停止（個股選擇權不列入本表）。
    回傳 [{code, ucode, name, ratio, kind}]
      kind: 'stock' | 'mini' | 'etf'
      ratio: 原始保證金適用比例 (0~1)
    """
    rows = []
    section = "stock"  # 預設標的為股票
    for line in text.splitlines():
        if not line.strip():
            continue
        # 碰到選擇權契約區塊就停止讀取
        if "選擇權契約" in line:
            break
        # 區段標題判斷（期貨）
        if "標的證券為" in line or "ＥＴＦ" in line or "ETF" in line:
            if "ETF" in line or "ＥＴＦ" in line:
                section = "etf"
            elif "股票" in line:
                section = "stock"
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
            rows = []
            matched = 0
            for r in parsed:
                mult = {"stock": MULT_STOCK, "mini": MULT_MINI,
                        "etf": MULT_ETF}[r["kind"]]
                price = price_map.get(r["code"])
                if price is None:
                    price = price_map.get(r["name"])
                vol = vol_map.get(r["code"]) or vol_map.get(r["name"]) or 0
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
                    "volume": vol,
                    "margin": margin,
                    "currency": "TWD",
                })
            # 排序：成交量降冪，次之股票代號升冪
            rows.sort(key=lambda x: (-x["volume"], x["ucode"]))
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


# 預設展開的分類（其餘預設摺疊）
OPEN_BY_DEFAULT = {"index"}


def render_simple_section(sec):
    is_open = " open" if sec["key"] in OPEN_BY_DEFAULT else ""
    parts = [f'<details class="cat" data-cat="{esc(sec["key"])}"{is_open}>']
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


def render_stock_section(sec):
    is_open = " open" if "stock" in OPEN_BY_DEFAULT else ""
    parts = [f'<details class="cat" data-cat="stock"{is_open}>']
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
    parts.append('<div class="searchbar">'
                 '<svg class="sicon" viewBox="0 0 24 24" aria-hidden="true">'
                 '<circle cx="11" cy="11" r="7"></circle>'
                 '<line x1="21" y1="21" x2="16.5" y2="16.5"></line></svg>'
                 '<input id="stockSearch" type="search" inputmode="search" '
                 'autocomplete="off" placeholder="搜尋：名稱或代號（例：台積電、2330）" />'
                 '<button type="button" id="stockClear" class="sclear" '
                 'aria-label="清除搜尋">×</button></div>')
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
        # 收盤價做成灰色備註，跟代號放同一行
        price_note = f'・收盤 {esc(fmt_price(r["price"]))}' if r["price"] is not None else ""
        sub = f'<span class="ucode">{esc(r["ucode"])}・×{r["mult"]:,}{price_note}</span>'
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
    parts.append('<p class="noresult" id="stockNoResult" hidden>找不到符合的股票期貨。</p>')
    parts.append('</div></div></details>')
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
<meta name="referrer" content="no-referrer">
<title>華南期貨商品保證金一覽</title>
<style>
  :root{
    --fs: 14px;
    --bg:#0a0e17; --bg2:#0e1422;
    --card:#121a2b; --card2:#0f1626;
    --line:#23304a; --line2:#2e3e5e;
    --fg:#e8eef9; --muted:#8593ab;
    --accent:#35e0d6;
    --accent-b:#4ea1ff;
    --accent-v:#a78bfa;
    --warn:#ffb454;
    --pos:#35e0d6;
    --th:#0e1626; --zebra:#0d1320;
    --mono: "SF Mono","JetBrains Mono",ui-monospace,"Roboto Mono","Cascadia Code",Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: light){
    :root{
      --bg:#eef2f8; --bg2:#e7ecf5;
      --card:#ffffff; --card2:#f7f9fd;
      --line:#dde4ef; --line2:#cfd9ea;
      --fg:#16203a; --muted:#5c6a85;
      --accent:#0bb3a8; --accent-b:#2f7ff0; --accent-v:#7c5cff;
      --warn:#c97a1a; --pos:#0a9d93;
      --th:#eef3fb; --zebra:#f5f8fd;
    }
  }
  *{ box-sizing:border-box; }
  html,body{ margin:0; padding:0; color:var(--fg);
    font-family:-apple-system,"Segoe UI",Roboto,"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;
    font-size:var(--fs); line-height:1.5; -webkit-text-size-adjust:100%; }
  body{
    background:
      radial-gradient(1100px 520px at 50% -8%, rgba(78,161,255,.10), transparent 60%),
      radial-gradient(820px 420px at 92% 4%, rgba(167,139,250,.08), transparent 55%),
      radial-gradient(820px 420px at 6% 8%, rgba(53,224,214,.07), transparent 55%),
      linear-gradient(180deg, var(--bg) 0%, var(--bg2) 100%);
    background-attachment: fixed;
    min-height:100vh;
  }
  .wrap{ max-width:840px; margin:0 auto; padding:0 12px 72px; }

  /* ---- 頂部固定列 ---- */
  header.bar{ position:sticky; top:0; z-index:30;
    background:linear-gradient(180deg, rgba(12,18,32,.94), rgba(12,18,32,.80));
    border-bottom:1px solid var(--line);
    padding:9px 12px 10px; margin:0 -12px 14px;
    backdrop-filter:saturate(1.3) blur(10px); -webkit-backdrop-filter:saturate(1.3) blur(10px);
  }
  @media (prefers-color-scheme: light){
    header.bar{ background:linear-gradient(180deg, rgba(255,255,255,.96), rgba(255,255,255,.82)); }
  }
  .bar-top{ display:flex; align-items:center; gap:8px; margin-bottom:8px; }
  .bar h1{ font-size:1.05rem; margin:0; display:flex; align-items:center; gap:8px;
    letter-spacing:.4px; font-weight:700; flex:1; }
  .bar h1 .logo{ width:20px;height:20px;flex:0 0 20px; }
  .bar h1 .ttl{
    background:linear-gradient(92deg, var(--accent), var(--accent-b) 55%, var(--accent-v));
    -webkit-background-clip:text; background-clip:text; color:transparent;
  }
  /* 社群 icon */
  .social{ display:flex; gap:10px; align-items:center; }
  .social a{ display:flex; align-items:center; justify-content:center;
    width:30px; height:30px; border-radius:8px; color:var(--muted);
    transition:color .15s, background .15s; text-decoration:none; }
  .social a:hover{ color:var(--fg); background:rgba(255,255,255,.08); }
  .social svg{ width:20px; height:20px; display:block; }

  .controls{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .fsgroup{ display:flex; gap:0; border:1px solid var(--line2); border-radius:10px; overflow:hidden; }
  .fsbtn{ border:0; border-right:1px solid var(--line2); background:transparent; color:var(--fg);
    padding:6px 10px; font-size:0.9rem; cursor:pointer; min-width:38px; transition:background .15s; }
  .fsbtn:last-child{ border-right:0; }
  .fsbtn:hover{ background:rgba(78,161,255,.12); }
  .fsbtn:active{ transform:scale(.95); }
  .fsbtn.mid{ font-weight:700; }
  .calc{ flex:1 1 200px; display:flex; align-items:center; gap:7px;
    border:1px solid var(--line2); border-radius:12px; padding:5px 10px;
    background:var(--card2); transition:border-color .18s, box-shadow .18s; }
  .calc:focus-within{ border-color:var(--accent); box-shadow:0 0 0 3px rgba(53,224,214,.16); }
  .calc label{ color:var(--muted); font-size:0.78rem; white-space:nowrap; }
  .calc input{ flex:1; min-width:0; border:0; background:transparent; color:var(--fg);
    font-size:1rem; font-family:var(--mono); font-variant-numeric:tabular-nums;
    outline:none; text-align:right; letter-spacing:.5px; }
  .calc .ccy{ color:var(--accent); font-size:0.78rem; font-weight:700; }
  .hint{ color:var(--muted); font-size:0.73rem; margin:6px 2px 0; line-height:1.45; }

  /* ---- 分類（可摺疊） ---- */
  details.cat{ background:linear-gradient(180deg, var(--card), var(--card2));
    border:1px solid var(--line); border-radius:14px; margin:0 0 12px; overflow:hidden;
    box-shadow:0 8px 24px -18px rgba(0,0,0,.6); }
  details.cat[open]{ border-color:var(--line2); }
  details.cat > summary{
    list-style:none; cursor:pointer; user-select:none;
    display:flex; align-items:center; gap:10px;
    padding:12px 14px; font-size:.95rem; font-weight:700; letter-spacing:.3px;
  }
  details.cat > summary::-webkit-details-marker{ display:none; }
  summary .cat-name{ position:relative; padding-left:12px; }
  summary .cat-name::before{ content:""; position:absolute; left:0; top:50%; transform:translateY(-50%);
    width:3px; height:1em; border-radius:3px;
    background:linear-gradient(180deg, var(--accent), var(--accent-b));
    box-shadow:0 0 8px rgba(53,224,214,.5); }
  summary .upd{ margin-left:auto; color:var(--muted); font-size:0.68rem; font-weight:400; white-space:nowrap; }
  summary .chev{ width:14px; height:14px; flex:0 0 14px; position:relative; }
  summary .chev::before, summary .chev::after{ content:""; position:absolute; top:6px; width:8px; height:2px;
    border-radius:2px; background:var(--muted); transition:transform .2s ease; }
  summary .chev::before{ left:0; transform:rotate(45deg); }
  summary .chev::after{ right:0; transform:rotate(-45deg); }
  details[open] > summary .chev::before{ transform:rotate(-45deg); }
  details[open] > summary .chev::after{ transform:rotate(45deg); }
  summary:hover .cat-name{ color:var(--accent); }
  .catbody{ border-top:1px solid var(--line); }

  .curnote{ color:var(--muted); font-size:0.73rem; margin:8px 12px; line-height:1.5; }
  .err{ color:var(--warn); font-size:0.82rem; margin:10px 12px; }
  .noresult{ color:var(--muted); font-size:0.82rem; margin:12px; text-align:center; }

  /* ---- 搜尋列 ---- */
  .searchbar{ display:flex; align-items:center; gap:7px; margin:10px 12px 4px;
    border:1px solid var(--line2); border-radius:10px; padding:6px 10px; background:var(--card2);
    transition:border-color .18s, box-shadow .18s; }
  .searchbar:focus-within{ border-color:var(--accent-b); box-shadow:0 0 0 3px rgba(78,161,255,.16); }
  .searchbar .sicon{ width:15px;height:15px;flex:0 0 15px; fill:none; stroke:var(--muted);
    stroke-width:2; stroke-linecap:round; }
  .searchbar input{ flex:1; min-width:0; border:0; background:transparent; color:var(--fg);
    font-size:0.88rem; outline:none; }
  .searchbar input::placeholder{ color:var(--muted); }
  .sclear{ border:0; background:transparent; color:var(--muted); font-size:1.1rem; line-height:1;
    cursor:pointer; padding:0 3px; visibility:hidden; }
  .searchbar.has-q .sclear{ visibility:visible; }

  /* ---- 表格 ---- */
  .tw{ width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  table{ width:100%; border-collapse:collapse; }
  th,td{ padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  thead th{ position:sticky; top:0; background:var(--th); font-size:0.7rem;
    color:var(--muted); font-weight:600; white-space:nowrap; z-index:1; letter-spacing:.2px; }
  th.l,td.l{ text-align:left; }
  th.r,td.r{ text-align:right; white-space:nowrap; }
  td.num{ font-family:var(--mono); font-variant-numeric:tabular-nums; }
  tbody tr:nth-child(even){ background:var(--zebra); }
  tbody tr.mrow{ transition:background .12s; }
  tbody tr.mrow:hover{ background:rgba(78,161,255,.07); }
  td.lots{ color:var(--accent); font-weight:700; text-shadow:0 0 10px rgba(53,224,214,.25); }
  td.lots.zero{ color:var(--warn); text-shadow:none; }
  td.vol{ color:var(--muted); }
  .cur{ display:inline-block; margin-left:5px; padding:0px 5px; border-radius:5px;
    background:rgba(255,180,84,.16); color:var(--warn); font-size:0.65rem; font-weight:700;
    font-family:var(--mono); }
  .kind{ display:inline-block; margin-left:5px; padding:0 5px; border-radius:5px;
    background:rgba(167,139,250,.18); color:var(--accent-v); font-size:0.63rem; font-weight:700; }
  .ucode{ display:block; color:var(--muted); font-size:0.68rem; margin-top:1px; font-family:var(--mono); }

  /* 股票表：緊湊欄位 */
  table.stock th, table.stock td{ padding:6px 6px; }
  table.stock td.l{ min-width:110px; max-width:160px; }
  table.stock th.r, table.stock td.r{ padding-left:4px; padding-right:6px; }

  footer{ color:var(--muted); font-size:0.7rem; text-align:center; padding:16px 8px 0; line-height:1.75; }
  footer a{ color:var(--accent-b); }
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
          <path d="M6 15l3.5-4 3 2.4L18 8" stroke="url(#lg)" stroke-width="1.9"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="ttl">華南期貨商品保證金一覽</span>
      </h1>
      <nav class="social" aria-label="社群">
        <a href="https://www.instagram.com/f1_futures/" target="_blank" rel="noopener noreferrer" aria-label="Instagram">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="2" width="20" height="20" rx="5"/>
            <circle cx="12" cy="12" r="4.5"/>
            <circle cx="17.5" cy="6.5" r="0.8" fill="currentColor" stroke="none"/>
          </svg>
        </a>
        <a href="https://www.threads.com/@f1_futures" target="_blank" rel="noopener noreferrer" aria-label="Threads">
          <svg viewBox="0 0 24 24" fill="currentColor">
            <path d="M19.59 13.57c-.16-1.01-.61-1.87-1.35-2.55-.78-.72-1.78-1.13-2.99-1.24-.07-.01-.14-.01-.21-.01-1.16 0-2.13.38-2.88 1.13-.74.74-1.12 1.72-1.12 2.9 0 1.15.36 2.1 1.07 2.81.72.72 1.68 1.08 2.86 1.08.86 0 1.61-.22 2.22-.65.6-.43 1.03-1.05 1.27-1.84l-1.62-.45c-.31.93-.96 1.39-1.87 1.39-.66 0-1.2-.19-1.62-.58-.41-.39-.62-.92-.62-1.59h5.93c.01-.12.02-.26.02-.4.01-.01.01-.01-.09 0zm-5.93-.47c.1-.56.34-1 .71-1.31.36-.31.83-.47 1.39-.47.57 0 1.04.16 1.4.47.36.31.57.75.63 1.31h-4.13zM12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2z"/>
          </svg>
        </a>
      </nav>
    </div>
    <div class="controls">
      <div class="fsgroup" role="group" aria-label="字級調整">
        <button class="fsbtn" id="fsDown" aria-label="縮小字級">A−</button>
        <button class="fsbtn mid" id="fsReset" aria-label="預設字級">A</button>
        <button class="fsbtn" id="fsUp" aria-label="放大字級">A＋</button>
      </div>
      <div class="calc">
        <label for="avail">可用保證金</label>
        <input id="avail" inputmode="numeric" autocomplete="off" placeholder="輸入金額" value="3,000,000" />
        <span class="ccy">NT$</span>
      </div>
    </div>
    <div class="hint">輸入「可用保證金」後，下方各表最右欄即時換算「可下單口數」（無條件捨去）。外幣計價商品需自行換匯，不計入口數。</div>
  </header>

  {{SECTIONS}}

  <footer>
    資料來源：臺灣期貨交易所（TAIFEX）保證金一覽表與每日交易行情（一般交易時段）。<br>
    資料每日自動更新；僅供參考，實際保證金以期交所及各期貨商公告為準。<br>
    產生時間：{{GEN_TW}}（台北）／{{GEN_UTC}}
  </footer>
</div>

<script>
(function(){
  // ---- 反爬：禁止右鍵選取與開發者工具快捷鍵 ----
  document.addEventListener('contextmenu', function(e){ e.preventDefault(); });
  document.addEventListener('keydown', function(e){
    if(e.key==='F12'||(e.ctrlKey&&e.shiftKey&&(e.key==='I'||e.key==='J'||e.key==='C'))||
       (e.ctrlKey&&e.key==='U')){ e.preventDefault(); }
  });
  document.addEventListener('selectstart', function(e){
    if(e.target.tagName!=='INPUT'&&e.target.tagName!=='TEXTAREA'){ e.preventDefault(); }
  });

  var root = document.documentElement;

  // ---- 字級調整 ----
  var sizes = [11,12,13,14,15,16,18,20];
  var idx = 3; // 預設 14px
  function applyFs(){ root.style.setProperty('--fs', sizes[idx] + 'px'); }
  document.getElementById('fsUp').addEventListener('click', function(){
    if(idx < sizes.length-1){ idx++; applyFs(); }
  });
  document.getElementById('fsDown').addEventListener('click', function(){
    if(idx > 0){ idx--; applyFs(); }
  });
  document.getElementById('fsReset').addEventListener('click', function(){ idx = 3; applyFs(); });

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
      if(ccy !== 'TWD'){ cell.textContent = '—'; continue; }
      if(m === '' || m === null){ cell.textContent = '—'; continue; }
      var margin = parseFloat(m);
      if(!(margin > 0)){ cell.textContent = '—'; continue; }
      var lots = Math.floor(avail / margin);
      cell.textContent = lots.toLocaleString();
      if(lots === 0){ cell.classList.add('zero'); }
    }
  }
  input.addEventListener('input', function(){
    var n = parseAmount(input.value);
    if(n !== null){ input.value = n.toLocaleString('en-US'); }
    update();
  });
  update();

  // ---- 股票期貨搜尋 ----
  var search = document.getElementById('stockSearch');
  if(search){
    var clearBtn = document.getElementById('stockClear');
    var bar = search.closest('.searchbar');
    var noResult = document.getElementById('stockNoResult');
    var stockDetails = search.closest('details.cat');
    var srows = Array.prototype.slice.call(document.querySelectorAll('tr.srow'));
    function norm(s){ return (s||'').toLowerCase().trim(); }
    function runFilter(){
      var q = norm(search.value);
      bar.classList.toggle('has-q', q.length > 0);
      if(stockDetails && q.length > 0 && !stockDetails.open){ stockDetails.open = true; }
      var shown = 0;
      for(var i=0;i<srows.length;i++){
        var key = srows[i].getAttribute('data-search') || '';
        var hit = (q === '' || key.indexOf(q) !== -1);
        srows[i].style.display = hit ? '' : 'none';
        if(hit) shown++;
      }
      if(noResult){ noResult.hidden = !(q !== '' && shown === 0); }
    }
    search.addEventListener('input', runFilter);
    clearBtn.addEventListener('click', function(){ search.value=''; runFilter(); search.focus(); });
  }
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
