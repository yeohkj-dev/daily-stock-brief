"""观察清单每日简报：盘前版 + 收盘版。规则整理，不用 AI（新闻标题和摘要用机器翻译成中文）。

每次运行：
  1. 读 watchlist.txt（股票清单）和 config.yml（设定）
  2. TradingView 取价格：现价、今日涨跌、盘前 / 盘后涨跌、量比、5 日 / 1 月 / 年初至今表现、财报日期
  3. Yahoo Finance 和 Google News 的 RSS 取每只股票的新闻，只留提到该公司的，去重后按来源和时间排序
  4. 涨跌超过门槛的股票：按新闻关键词归类原因（财报、评级、订单、融资……），并和大盘 / 板块对比
  5. 输出 docs/index.html（最新）和 docs/archive/<日期>-<版本>.html

用法：python tools/stock_brief.py [--mode premarket|close] [--force]
  不加 --mode：按美东时间自动判断（04:00–09:29 盘前版，16:00–20:59 收盘版，其余时间跳过）
  --force：不在时间窗口、休市或今天已经出过同一版，也照样生成（手动测试用）
仅供学习，不构成投资建议。
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
ARCH = os.path.join(DOCS, "archive")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
SCAN = "https://scanner.tradingview.com/america/scan"
e = lambda s: html.escape(str(s if s is not None else ""))

# 美股休市日（NYSE）。每年底补下一年。
HOLIDAYS = {"2026-11-26", "2026-12-25",
            "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
            "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
BENCH = [("SPY", "标普 500"), ("QQQ", "纳指 100"), ("SMH", "半导体"), ("IWM", "罗素 2000")]
SEMI = {"Semiconductors", "Electronic Production Equipment"}
ALIAS = {"GOOG": ["Alphabet", "Google"], "GOOGL": ["Alphabet", "Google"], "META": ["Meta", "Facebook"],
         "HOOD": ["Robinhood"], "RKLB": ["Rocket Lab"], "ACHR": ["Archer Aviation", "Archer"], "LITE": ["Lumentum"],
         "AAOI": ["Applied Optoelectronics"], "MCHP": ["Microchip"], "ONDS": ["Ondas"], "SPMO": ["Momentum ETF", "SPMO"],
         "AVGO": ["Broadcom"], "PLTR": ["Palantir"], "IBM": ["IBM"], "AAPL": ["Apple"], "INTC": ["Intel"],
         "AMD": ["AMD", "Advanced Micro"], "NVDA": ["Nvidia"], "ORCL": ["Oracle"]}
TRUSTED = ("Reuters", "Bloomberg", "Wall Street Journal", "WSJ", "CNBC", "MarketWatch", "Financial Times", "Barron's",
           "Associated Press", "Yahoo Finance", "Investor's Business Daily", "Investopedia", "Fox Business", "Axios",
           "Business Insider", "Forbes", "Morningstar", "Nasdaq", "TheStreet", "Benzinga", "Motley Fool", "Seeking Alpha",
           "The Information", "Tom's Hardware", "CNN", "The Verge", "TechCrunch")
NOISE = re.compile(r"should you buy|stocks? to (buy|watch)|is it (too late|time)|better buy|millionaire|"
                   r"\bforever\b|could make you|prediction:|\bdividend stocks?\b|retire|top \d+ |best .* stocks|"
                   r"market report|researchandmarkets|strategic business report|\bcagr\b|market size|forecast to 20\d\d|"
                   r"worth buying|still a buy|buy (it )?now\b|falling knife|time to buy|\bbuy the dip\b|stocks? (that )?could soar|"
                   r"hedge funds?.* (buying|selling)|\b13f\b.*(adds|trims)|accelerates \+\d", re.I)
# 标题里有「涨 / 跌 / 为什么」的报道，最可能直接解释股价变动
MOVE_RX = re.compile(r"\b(why|jump|soar|surge|rall(y|ies|ied)|climb|gain|rise|rises|rose|pop|spike|skyrocket|"
                     r"fall|falls|fell|drop|sink|sinks|sank|slump|plunge|tumble|slide|slides|slid|crash|sell-?off|rout|"
                     r"shares? (up|down)|stock (up|down)|(up|down) \d+(\.\d+)?%|\d+(\.\d+)?% (higher|lower))", re.I)

# 原因归类：(标签, 正则)。按顺序显示，前面的更直接。
REASONS = [
    ("📊 财报 / 业绩", r"earnings|quarterly results|\bq[1-4]\b|revenue|\beps\b|beats?\b|miss(es|ed)?\b|profit|results"),
    ("🧭 指引 / 展望", r"guidance|outlook|forecast|raises (its )?(full-year|annual)|cuts (its )?(full-year|annual)"),
    ("⬆️ 分析师看好", r"upgrade|raises? (its )?price target|target raised|outperform|overweight|buy rating|initiat\w* .*buy|bullish"),
    ("⬇️ 分析师看淡", r"downgrade|cuts? (its )?price target|lowers? (its )?price target|target cut|underperform|underweight|sell rating|bearish"),
    ("🤝 订单 / 合作", r"contract|deal\b|partnership|partners? with|agreement|\border\b|awarded|wins?\b|selected|supply"),
    ("🔀 并购", r"acqui|merger|takeover|buyout|to buy\b|bid for"),
    ("💵 融资 / 增发", r"offering|convertible|notes due|raise[sd]? \$|capital raise|share sale|dilut|at-the-market|\batm\b"),
    ("👤 内部人 / 大股东", r"insider|stake|sells? shares|bought shares|13f|ceo (sells|buys)|resign|steps down|appoint"),
    ("⚖️ 监管 / 诉讼", r"\bsec\b|probe|investigat|lawsuit|sued|regulat|antitrust|\bfaa\b|\bfda\b|export (control|curb|ban)|ban\b"),
    ("🚀 产品 / 技术", r"launch|unveil|introduc|release[sd]?\b|new chip|\bmodel\b|test flight|certification|milestone"),
    ("🌐 宏观 / 政策", r"tariff|\bfed\b|rate cut|inflation|election|white house|trump|china|jobs report|treasury"),
    ("🌊 板块 / 资金流向", r"sell-?off|rout|sector|peers|ai (trade|stocks|bubble|selloff)|chip stocks|momentum|rotation|bubble"),
]


# ---------------- 通用 ----------------
def fetch(url, data=None, headers=None, timeout=30, tries=3):
    h = {"User-Agent": UA}
    h.update(headers or {})
    last = None
    for k in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout).read().decode("utf-8", "replace")
        except Exception as ex:
            last = ex
            time.sleep(1.5 * (k + 1))
    raise last


def et_offset(d):
    """美东：3 月第 2 个周日到 11 月第 1 个周日是夏令时（UTC-4），其余 UTC-5。"""
    y = d.year
    mar = dt.date(y, 3, 8) + dt.timedelta(days=(6 - dt.date(y, 3, 8).weekday()) % 7)
    nov = dt.date(y, 11, 1) + dt.timedelta(days=(6 - dt.date(y, 11, 1).weekday()) % 7)
    return dt.timezone(dt.timedelta(hours=-4 if mar <= d.date() < nov else -5))


def to_et(t):
    return t.astimezone(et_offset(t))


def read_watchlist():
    out = []
    for line in open(os.path.join(ROOT, "watchlist.txt"), encoding="utf-8-sig"):
        s = line.split("#", 1)[0].strip().upper()
        if s and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) and s not in out:
            out.append(s)
    return out


def read_config():
    cfg = dict(big_move_pct=3.0, news_per_stock=4, news_hours=24, translate=True, keep_archive=120, telegram="close")
    p = os.path.join(ROOT, "config.yml")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8-sig"):
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            if k in cfg:
                d = cfg[k]
                if isinstance(d, str):
                    cfg[k] = v.lower() or d
                else:
                    cfg[k] = (v.lower() in ("true", "yes", "1")) if isinstance(d, bool) else type(d)(float(v)) if v else d
    return cfg


# ---------------- 价格（TradingView）----------------
COLS = ["name", "description", "close", "change", "premarket_change", "premarket_close", "postmarket_change",
        "postmarket_close", "volume", "average_volume_10d_calc", "relative_volume_10d_calc", "Perf.W", "Perf.1M",
        "Perf.YTD", "market_cap_basic", "sector", "industry", "earnings_release_date", "earnings_release_next_date",
        "type", "high", "low", "gap", "SMA50", "SMA200"]


def tv_quotes(syms):
    tickers = [f"{x}:{s}" for s in syms for x in ("NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "CBOE")]
    body = json.dumps({"symbols": {"tickers": tickers}, "columns": COLS}).encode()
    rows = json.loads(fetch(SCAN, data=body, headers={"Content-Type": "application/json"}))["data"]
    out = {}
    for r in rows:
        d = dict(zip(COLS, r["d"]))
        d["exchange"] = r["s"].split(":")[0]
        out.setdefault(d["name"], d)          # 同名只取第一个（NASDAQ 优先）
    return out


# ---------------- 阶段（Stan Weinstein / J Law 的 Stage 1–4）----------------
SLOPE_UP, SLOPE_DN = 1.0, -1.5    # 200 天线近 21 日变动 %：≥1 向上，≤-1.5 向下，中间走平（和 jlaw-daily-report 一致）
STAGE_NAME = {1: "Stage 1 底部整理", 2: "Stage 2 上升阶段", 3: "Stage 3 顶部整理", 4: "Stage 4 下跌阶段"}
STAGE_CLS = {1: "s1", 2: "s2", 3: "s3", 4: "s4"}


def sma(v, n, back=0):
    end = len(v) - back
    return sum(v[end - n:end]) / n if end >= n else None


def stage_of(c):
    """c = 日收盘价（旧→新）。返回阶段、趋势和判断依据。
    Stage 2：收盘 > 200 天线、50 天线 > 200 天线、200 天线向上
    Stage 4：收盘 < 200 天线、50 天线 < 200 天线、200 天线向下
    其余（200 天线走平或信号矛盾）：之前是上升 → Stage 3；之前是下跌 → Stage 1。"""
    if len(c) < 252:
        return None
    close = c[-1]
    s50, s200 = sma(c, 50), sma(c, 200)
    sl200 = (s200 / sma(c, 200, 21) - 1) * 100
    sl50 = (s50 / sma(c, 50, 10) - 1) * 100
    prior = (sma(c, 200, 42) / sma(c, 200, 168) - 1) * 100 if len(c) >= 368 else (sma(c, 200, 42) / sma(c, 200, 52) - 1) * 100 * 12
    hi52, lo52 = max(c[-252:]), min(c[-252:])
    up200, dn200 = sl200 >= SLOPE_UP, sl200 <= SLOPE_DN
    if close > s200 and up200:
        stg = 2
        if s50 > s200:
            why = "收盘在 200 天线上、50 天线在 200 天线上、200 天线向上"
            sub = "健康上升" if close > s50 else "上升中回调（跌破 50 天线）"
        else:
            why = "收盘在 200 天线上、200 天线向上，但 50 天线还没上穿 200 天线"
            sub = "早期 Stage 2（50 天线未上穿）"
    elif close < s200 and dn200:
        stg = 4
        if s50 < s200:
            why = "收盘在 200 天线下、50 天线在 200 天线下、200 天线向下"
            sub = "持续下跌" if close < s50 else "下跌中反弹（站上 50 天线）"
        else:
            why = "收盘在 200 天线下、200 天线向下，但 50 天线还没下穿 200 天线"
            sub = "刚转入 Stage 4（50 天线未下穿）"
    else:
        stg = 3 if prior > 0 else 1
        pos = "上" if close > s200 else "下"
        d200 = "向上" if up200 else "向下" if dn200 else "走平"
        why = (f"200 天线{d200}、收盘在 200 天线{pos}，信号未一致；之前 6 个月 200 天线"
               f"{'上升' if prior > 0 else '下降'} {prior:+.1f}% → " + ("由升转弱，可能在做顶" if stg == 3 else "跌后横盘，可能在筑底"))
        if stg == 3:
            sub = "跌破 200 天线，有转入 Stage 4 风险" if close < s200 else "仍在 200 天线上，涨势放缓"
        else:
            sub = "站上 200 天线，留意转入 Stage 2" if close > s200 else "仍在 200 天线下"
    mid = "上升" if close > s50 and sl50 > 0 else "下跌" if close < s50 and sl50 < 0 else "震荡"
    long_ = "上升" if up200 else "下跌" if dn200 else "走平"
    return dict(stage=stg, name=STAGE_NAME[stg], sub=sub, why=why, mid=mid, long=long_, close=close, s50=s50, s200=s200,
                sl50=sl50, sl200=sl200, to50=(close / s50 - 1) * 100, to200=(close / s200 - 1) * 100,
                from_hi=(close / hi52 - 1) * 100, from_lo=(close / lo52 - 1) * 100, sma_ok=True)


# ---------------- 数据核对（Yahoo 交叉对照）----------------
def last_session(et_now, mode):
    """应对照的最近一个已收盘交易日：美东 16:00 以后 = 今天，之前 = 前一个交易日（盘前版因此对照昨收）。"""
    d = et_now.date() if et_now.hour >= 16 else et_now.date() - dt.timedelta(days=1)
    while d.weekday() >= 5 or str(d) in HOLIDAYS:
        d -= dt.timedelta(days=1)
    return d


def yahoo_daily(sym):
    r = json.loads(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=2y&interval=1d"))
    res = r["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    bars = [(to_et(dt.datetime.fromtimestamp(t, dt.timezone.utc)).date(), c) for t, c in zip(res["timestamp"], closes) if c]
    return bars


def verify(syms, quotes, mode, et_now):
    """逐只核对：TradingView 收盘价、涨跌幅 vs Yahoo 日线；数据日期是否是应有的交易日；盘前价与盘前涨幅是否一致。
    返回 {代号: dict(ok, level, notes)}，level = ok / warn / fail。"""
    want = last_session(et_now, mode)

    def one(s):
        q = quotes.get(s)
        notes, level = [], "ok"
        if not q:
            return s, dict(level="fail", notes=["TradingView 取不到"])
        try:
            bars = yahoo_daily(s)
        except Exception as ex:
            return s, dict(level="warn", notes=[f"Yahoo 取不到，无法交叉核对（{type(ex).__name__}）"])
        bars = [b for b in bars if b[0] <= want]
        if len(bars) < 2:
            return s, dict(level="warn", notes=["Yahoo 数据不足"])
        (d1, c1), (_, c0) = bars[-1], bars[-2]
        if d1 != want:
            level = "fail"
            notes.append(f"Yahoo 最新收盘是 {d1}，应为 {want}（数据可能未更新）")
        tc, tch = q.get("close"), q.get("change")
        if tc is None or abs(tc / c1 - 1) > 0.005:
            level = "fail"
            notes.append(f"收盘价不符：TradingView {tc} vs Yahoo {c1:.2f}")
        ych = (c1 / c0 - 1) * 100
        if tch is None or abs(tch - ych) > 0.3:
            level = "fail" if tch is None or abs(tch - ych) > 1 else ("warn" if level == "ok" else level)
            notes.append(f"涨跌幅不符：TradingView {tch if tch is None else round(tch, 2)}% vs Yahoo {ych:.2f}%")
        if mode == "premarket":
            pm, pmc = q.get("premarket_change"), q.get("premarket_close")
            if pm is None:
                notes.append("盘前暂无成交")
            elif pmc and tc and abs((pmc / tc - 1) * 100 - pm) > 0.3:
                level = "fail"
                notes.append(f"盘前价 {pmc} 相对昨收 {tc} 是 {(pmc / tc - 1) * 100:+.2f}%，与盘前涨幅 {pm:+.2f}% 对不上"
                             "（盘前数据可能是旧的）")
        price_ok = not any(n.startswith(("收盘价", "涨跌幅", "Yahoo 最新")) for n in notes)
        st = stage_of([c for _, c in bars])
        if st:   # 用 Yahoo 算的 50 / 200 天线和 TradingView 的对照
            for k, n in (("SMA50", "s50"), ("SMA200", "s200")):
                tv = q.get(k)
                if tv is None or abs(st[n] / tv - 1) > 0.01:
                    level = "warn" if level == "ok" else level
                    notes.append(f"{k[3:]} 天线不符：Yahoo 算 {st[n]:.2f} vs TradingView {tv}（阶段判断请谨慎）")
                    st["sma_ok"] = False
        else:
            notes.append("历史不足 200 天，无法判断阶段")
        if not notes:
            notes.append(f"收盘 {c1:.2f}、涨跌 {ych:+.2f}% 与 Yahoo 一致（{d1}）")
        return s, dict(level=level, notes=notes, yclose=c1, ychange=ych, price_ok=price_ok, stage=st)

    with ThreadPoolExecutor(6) as ex:
        return dict(ex.map(one, syms)), want


# ---------------- 新闻 ----------------
def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_rss(text, default_src):
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title"))
        src_el = it.find("source")
        src = src_el.text.strip() if src_el is not None and src_el.text else default_src
        if default_src == "Google News" and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        desc = strip_tags(it.findtext("description"))
        if default_src == "Google News":
            desc = ""                          # Google 的摘要只是重复标题
        desc = re.sub(r"^Key Takeaways\s*", "", desc)
        try:
            when = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        out.append(dict(title=title, desc=desc, link=(it.findtext("link") or "").strip(), src=src, when=when))
    return out


def short_name(desc):
    s = re.sub(r",? (Inc|Corp|Corporation|Holdings|Ltd|Limited|Company|Co|plc|Class [A-C]|Technologies|Technology|Group)\.?\b", "", desc or "", flags=re.I)
    return s.split(" ")[0] if len(s.split(" ")) > 2 else s.strip()


def names_for(sym, q):
    names = list(ALIAS.get(sym, []))
    sn = short_name(q.get("description", "")) if q else ""
    if sn and sn not in names and len(sn) > 2:
        names.append(sn)
    return names


def get_news(sym, names, since):
    items = []
    try:
        items += parse_rss(fetch(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"), "Yahoo Finance")
    except Exception:
        pass
    q = (f'"{names[0]}"' if names else sym) + (f" OR {sym}" if len(sym) >= 3 else "") + " stock when:2d"
    try:
        items += parse_rss(fetch("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"), "Google News")
    except Exception:
        pass
    pat = re.compile(r"\b(" + "|".join(re.escape(x) for x in [sym] + names) + r")\b", re.I)
    seen, out = [], []
    for it in sorted(items, key=lambda x: x["when"], reverse=True):
        if it["when"] < since or NOISE.search(it["title"]):
            continue
        if not pat.search(it["title"]) and not pat.search(it["desc"][:300]):
            continue
        toks = set(re.findall(r"[a-z0-9]{3,}", it["title"].lower()))
        if any(len(toks & t) / max(1, min(len(toks), len(t))) > 0.6 for t in seen):
            continue                            # 同一事件不同媒体：只留一条
        seen.append(toks)
        it["in_title"] = bool(pat.search(it["title"]))
        it["trusted"] = any(t.lower() in it["src"].lower() for t in TRUSTED)
        out.append(it)
    return out


def rank(news, n):
    def score(it):
        age_h = (dt.datetime.now(dt.timezone.utc) - it["when"]).total_seconds() / 3600
        return (2 if it["in_title"] else 0) + (1.5 if it["trusted"] else 0) + (1 if it["desc"] else 0) - age_h / 12
    return sorted(news, key=score, reverse=True)[:n]


def translate(texts):
    """Google 免费翻译，一次送多行；失败就保留英文。"""
    texts = [t for t in texts if t]
    out = {}
    chunk = []

    errors = []
    GTX = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t"

    def gtx(q, post):
        if post:   # POST：网址不会太长，也比较不容易被挡
            r = fetch(GTX, data=urllib.parse.urlencode({"q": q}).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, tries=2)
        else:
            r = fetch(GTX + "&q=" + urllib.parse.quote(q), tries=2)
        return "".join(seg[0] for seg in json.loads(r)[0] if seg[0])

    def dict_ext(items):
        """备用：Chrome 扩展用的翻译接口，一次可送多条。"""
        url = ("https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=en&tl=zh-CN&"
               + "&".join("q=" + urllib.parse.quote(t) for t in items))
        r = json.loads(fetch(url, tries=2))
        return [x[0] if isinstance(x, list) else x for x in r]

    def run(ch):
        q = "\n".join(t.replace("\n", " ") for t in ch)
        for post in (False, True):
            try:
                zh = gtx(q, post).split("\n")
                if len(zh) == len(ch):
                    out.update(zip(ch, (z.strip() for z in zh)))
                    return
                errors.append(f"行数不符 {len(zh)}/{len(ch)}")
                break
            except Exception as ex:
                errors.append(f"gtx{'-POST' if post else ''} {type(ex).__name__}: {str(ex)[:60]}")
        for i in range(0, len(ch), 8):
            part = ch[i:i + 8]
            try:
                zh = dict_ext(part)
                if len(zh) == len(part):
                    out.update(zip(part, (z.strip() for z in zh)))
            except Exception as ex:
                errors.append(f"dict-ext {type(ex).__name__}: {str(ex)[:60]}")

    size = 0
    for t in texts:
        if chunk and size + len(t) > 1800:
            run(chunk)
            chunk, size = [], 0
        chunk.append(t)
        size += len(t) + 1
    if chunk:
        run(chunk)
    if errors:
        print(f"翻译问题（{len(errors)}）：" + "；".join(sorted(set(errors))[:6]))
    return out


# ---------------- 分析 ----------------
def tags_of(it, with_desc=False):
    text = it["title"] + (" " + it["desc"][:300] if with_desc else "")
    return [tag for tag, rx in REASONS if re.search(rx, text, re.I)]


def reasons_from(news):
    """新闻里出现过的原因类别（只看标题，用于新闻卡片上的小标签）。"""
    found = []
    for it in news:
        for t in tags_of(it):
            if t not in [x for x, _ in found]:
                found.append((t, it))
    return [(t, it) for tag, _ in REASONS for t, it in found if t == tag]


UP_RX = re.compile(r"\b(jump|soar|surge|rall(y|ies|ied)|climb|gain|rise|rises|rose|pop|spike|skyrocket|higher|up \d)", re.I)
DN_RX = re.compile(r"\b(fall|falls|fell|drop|sink|sinks|sank|slump|plunge|tumble|slide|slides|slid|crash|sell-?off|rout|lower|down \d)", re.I)


def explainers(news, move=None):
    """挑出最能解释涨跌的报道；每条报道只出现一次。顺序：
    1) 标题点名该股、讲的涨跌方向和实际一致；2) 标题点名该股、讲「为什么」但没说方向；3) 标题点名该股、带原因类别。
    标题讲的方向和实际相反的（例如股价跌、标题说涨）不当作原因。"""
    same, opp = (DN_RX, UP_RX) if (move or 0) < 0 else (UP_RX, DN_RX)
    named = [it for it in news if it["in_title"]]
    a1 = [it for it in named if same.search(it["title"])]
    a2 = [it for it in named if it not in a1 and MOVE_RX.search(it["title"]) and not opp.search(it["title"])]
    b = [it for it in named if it not in a1 + a2 and tags_of(it) and not (opp.search(it["title"]) and not same.search(it["title"]))]
    a = a1 + a2
    return [(it, tags_of(it, with_desc=it in a)) for it in (a + b)[:3]]


def earnings_note(q, now):
    notes = []
    last, nxt = q.get("earnings_release_date"), q.get("earnings_release_next_date")
    if last:
        d = (now - dt.datetime.fromtimestamp(last, dt.timezone.utc)).total_seconds() / 86400
        if 0 <= d <= 3:
            notes.append(f"刚公布财报（{to_et(dt.datetime.fromtimestamp(last, dt.timezone.utc)):%m-%d}）")
    if nxt:
        d = (dt.datetime.fromtimestamp(nxt, dt.timezone.utc) - now).total_seconds() / 86400
        if 0 <= d <= 10:
            notes.append(f"财报将在 {to_et(dt.datetime.fromtimestamp(nxt, dt.timezone.utc)):%m-%d} 公布")
    return notes


def move_of(q, mode):
    """本版要分析的主要变动。盘前版 = 盘前相对昨收；收盘版 = 今日正常交易时段。"""
    if mode == "premarket":
        return q.get("premarket_change")
    return q.get("change")


def explain(sym, q, mode, bench, news, th, now):
    mv = move_of(q, mode)
    if mv is None:
        return None
    bsym = "SMH" if q.get("industry") in SEMI else "QQQ"
    bmv = move_of(bench.get(bsym, {}), mode)
    lines = []
    ex = explainers(news, mv)
    er = earnings_note(q, now)
    if er:
        lines.append(dict(text="📅 " + "；".join(er)))
    for it, tg in ex:
        d = it.get("zh_desc") or ""
        lines.append(dict(tags=" ".join(tg[:3]) or "📰 报道", text=it.get("zh_title") or it["title"], link=it["link"],
                          src=it["src"], when=it["when"], desc=(d[:110] + "…") if len(d) > 110 else d))
    tags = ex
    ctx = ""
    if bmv is not None:
        excess = mv - bmv
        ctx = f"同期 {bsym} {bmv:+.1f}%，个股{'多' if excess * mv > 0 else '少'}{'涨' if mv > 0 else '跌'} {abs(excess):.1f} 个百分点"
        if abs(bmv) >= 1 and abs(excess) < max(1.5, abs(mv) * 0.35):
            ctx += " → 主要跟随大盘 / 板块"
        elif abs(excess) >= th:
            ctx += " → 个股因素为主"
    if not tags and not er:
        lines.append(dict(text="没找到明确的个股消息" + ("，可能随大盘 / 板块或资金流向" if ctx else "")))
    rv = q.get("relative_volume_10d_calc")
    if rv and mode == "close" and rv >= 1.5:
        ctx += f"；成交量是 10 日均量的 {rv:.1f} 倍"
    return dict(sym=sym, move=mv, lines=lines, ctx=ctx, big=abs(mv) >= th)


# ---------------- HTML ----------------
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d1f23;--mut:#6b7280;--line:#e5e7eb;--up:#15803d;--dn:#b91c1c;--acc:#1f4e78;--chip:#eef2f7;--hl:#fff8e6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111317;--card:#1a1d23;--fg:#e6e8eb;--mut:#9aa1ab;--line:#2c313a;--up:#4ade80;--dn:#f87171;--acc:#8ab4f8;--chip:#252a33;--hl:#2a2617}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:4px 0}h2{font-size:18px;margin:26px 0 10px;color:var(--acc)}
.sub{color:var(--mut);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:12.5px}th:first-child,td:first-child{text-align:left}.tw{overflow-x:auto}
.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}.big{background:var(--hl)}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12.5px}
.s1{background:#e8eef7;color:#1f4e78}.s2{background:#dcfce7;color:#166534}.s3{background:#fef3c7;color:#92400e}.s4{background:#fee2e2;color:#991b1b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .s1{background:#1e2a3a;color:#93c5fd}:root:not([data-theme=light]) .s2{background:#14301f;color:#86efac}:root:not([data-theme=light]) .s3{background:#3a2e12;color:#fcd34d}:root:not([data-theme=light]) .s4{background:#3b1a1a;color:#fca5a5}}
.sym{font-weight:700;font-size:16px}a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
ul{margin:6px 0;padding-left:20px}li{margin:4px 0}.en{color:var(--mut);font-size:12.5px}
.nav{display:flex;gap:14px;flex-wrap:wrap;font-size:14px;margin:8px 0}.foot{color:var(--mut);font-size:12px;margin:28px 0 10px}
"""


def pc(x, d=1):
    if x is None:
        return '<span class="mut">—</span>'
    return f'<span class="{"up" if x > 0 else "dn" if x < 0 else "mut"}">{x:+.{d}f}%</span>'


MYT = dt.timezone(dt.timedelta(hours=8))


def when_txt(w):
    """新闻发布时间：美东 + 马来西亚时间 + 距今多久。"""
    t, m = to_et(w), w.astimezone(MYT)
    h = (dt.datetime.now(dt.timezone.utc) - w).total_seconds() / 3600
    ago = f"{h * 60:.0f} 分钟前" if h < 1 else f"{h:.0f} 小时前" if h < 48 else f"{h / 24:.0f} 天前"
    return f"{t:%Y-%m-%d %H:%M} 美东 · 大马 {m:%m-%d %H:%M} · {ago}"


def stage_chip(st):
    if not st:
        return '<span class="mut">—</span>'
    warn = "" if st.get("sma_ok", True) else " ⚠"
    return f'<span class="chip {STAGE_CLS[st["stage"]]}">Stage {st["stage"]}{warn}</span>'


def trend_txt(st):
    if not st:
        return '<span class="mut">—</span>'
    arrow = {"上升": '<span class="up">↗ 上升</span>', "下跌": '<span class="dn">↘ 下跌</span>'}
    return (arrow.get(st["mid"], f'<span class="mut">→ {st["mid"]}</span>') + " / "
            + arrow.get(st["long"], f'<span class="mut">→ {st["long"]}</span>'))


def price(x):
    return "—" if x is None else (f"{x:,.2f}" if x >= 1 else f"{x:.4f}")


def page(title, body):
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><div class=\"wrap\">{body}</div></body></html>")


def check_card(syms, checks, want, news_chk, missing):
    ok = [s for s in syms if checks.get(s, {}).get("level") == "ok"]
    warn = [s for s in syms if checks.get(s, {}).get("level") == "warn"]
    fail = [s for s in syms if checks.get(s, {}).get("level") == "fail"]
    bmark = all(checks.get(s, {}).get("level") == "ok" for s, _ in BENCH)
    pok = sum(bool(checks.get(s, {}).get("price_ok")) for s in syms)
    head = ("✅ 全部通过" if not warn and not fail and bmark else "⚠ 有需要注意的地方" if not fail else "❌ 有数据对不上，请谨慎参考")
    li = [f"<li>收盘价：{pok}/{len(syms)} 只的收盘价和涨跌幅与 Yahoo Finance 一致（对照交易日 {want}，"
          f"容许误差：价格 0.5%、涨跌幅 0.3 个百分点）；大盘 ETF {'全部通过' if bmark else '有差异'}</li>"]
    for s in warn + fail:
        li.append(f"<li>{'⚠' if s in warn else '❌'} <b>{s}</b>：{e('；'.join(checks[s]['notes']))}</li>")
    nc = news_chk
    li.append(f"<li>新闻：共 {nc['total']} 条，全部点名该公司（{nc['in_title']} 条在标题），最旧 {nc['oldest_h']:.0f} 小时前"
              + (f"；没有新闻的：{', '.join(nc['stocks_without'])}" if nc["stocks_without"] else "") + "</li>")
    if nc["translated"] is not None:
        li.append(f"<li>翻译：{nc['translated']}/{nc['total']} 条标题已翻成中文（没翻到的显示英文原文）</li>")
    if missing:
        li.append(f"<li>❌ 取不到数据：{', '.join(missing)}（代号可能有误）</li>")
    return f'<details class="card"{" open" if warn or fail else ""}><summary><b>🔎 数据核对：{head}</b></summary><ul>{"".join(li)}</ul></details>'


def build_html(mode, now_et, syms, quotes, bench, news_map, expl, cfg, missing, checks, want, news_chk):
    label = "盘前版" if mode == "premarket" else "收盘版"
    th = cfg["big_move_pct"]
    W = "一二三四五六日"
    B = [f'<div class="nav"><a href="./">最新报告</a><a href="archive/">历史报告</a></div>' if True else "",
         f"<h1>📈 观察清单 · {label}</h1>",
         f'<div class="sub">{now_et:%Y-%m-%d}（周{W[now_et.weekday()]}）美东 {now_et:%H:%M} 生成 · 马来西亚时间 '
         f"{now_et.astimezone(dt.timezone(dt.timedelta(hours=8))):%m-%d %H:%M} · 共 {len(syms)} 只</div>"]

    # 大盘
    bl = []
    for s, n in BENCH:
        q = bench.get(s)
        if q:
            bl.append(f'<span class="chip">{n} {s} {pc(move_of(q, mode))}</span>')
    mvs = [move_of(quotes[s], mode) for s in syms if s in quotes and move_of(quotes[s], mode) is not None]
    summ = ""
    if mvs:
        up, dn = sum(m > 0 for m in mvs), sum(m < 0 for m in mvs)
        nb = sum(abs(m) >= cfg["big_move_pct"] for m in mvs)
        summ = (f'<div style="margin-top:6px">观察清单：涨 <b class="up">{up}</b> · 跌 <b class="dn">{dn}</b> · '
                f"平均 {pc(sum(mvs) / len(mvs))} · 大涨大跌 {nb} 只</div>")
    B.append(f'<div class="card"><b>大盘{"（盘前）" if mode == "premarket" else "（今日）"}</b><br>{"".join(bl)}{summ}</div>')
    B.append(check_card(syms, checks, want, news_chk, missing))

    # 1. 动向总表
    mv_col = "盘前" if mode == "premarket" else "今日"
    ex_col = "昨日" if mode == "premarket" else "盘后"
    order = sorted([s for s in syms if s in quotes], key=lambda s: -(move_of(quotes[s], mode) or 0))
    rows = ""
    for s in order:
        q = quotes[s]
        mv = move_of(q, mode)
        px = q.get("premarket_close") if mode == "premarket" and q.get("premarket_close") else q.get("close")
        ex = q.get("change") if mode == "premarket" else q.get("postmarket_change")
        rv = q.get("relative_volume_10d_calc")
        flag = "🔥" if mv is not None and abs(mv) >= th else ""
        lv = checks.get(s, {}).get("level")
        ck = {"ok": "", "warn": " ⚠", "fail": " ❌"}.get(lv, "")
        st = checks.get(s, {}).get("stage")
        rows += (f'<tr class="{"big" if flag else ""}"><td><a href="#{s}"><b>{s}</b></a>{ck} {flag}</td><td>{price(px)}</td>'
                 f"<td>{pc(mv)}</td><td>{pc(ex)}</td><td>{stage_chip(st)}</td><td>{trend_txt(st)}</td>"
                 f"<td>{pc(q.get('Perf.W'))}</td><td>{pc(q.get('Perf.1M'))}</td>"
                 f"<td>{pc(q.get('Perf.YTD'))}</td><td>{'—' if rv is None else f'{rv:.1f}x'}</td></tr>")
    B.append("<h2>1. 每日动向</h2>")
    B.append(f'<div class="card tw"><table><tr><th>代号</th><th>价格</th><th>{mv_col}</th><th>{ex_col}</th><th>阶段</th>'
             f"<th>趋势 中期/长期</th><th>5日</th><th>1月</th><th>年初至今</th><th>量比</th></tr>{rows}</table>"
             f'<div class="sub" style="margin-top:6px">🔥 = {mv_col}涨跌超过 ±{th:g}%。'
             + ("盘前价来自盘前成交，流动性低，仅供参考。" if mode == "premarket" else "量比 = 今日成交量 ÷ 10 日均量。")
             + (f" ⚠ 找不到：{', '.join(missing)}" if missing else "") + "</div></div>")

    # 2. 阶段与趋势
    B.append("<h2>2. 阶段与趋势（Stage 1–4）</h2>")
    cnt = {k: [s for s in syms if (checks.get(s, {}).get("stage") or {}).get("stage") == k] for k in (1, 2, 3, 4)}
    B.append('<div class="card">' + " ".join(f'<span class="chip {STAGE_CLS[k]}">{STAGE_NAME[k]}：{len(v)} 只</span>'
                                              for k, v in cnt.items()) + "</div>")
    srows = ""
    for k in (2, 1, 3, 4):
        for s in sorted(cnt[k], key=lambda s: -checks[s]["stage"]["to200"]):
            st = checks[s]["stage"]
            srows += (f'<tr><td><a href="#{s}"><b>{s}</b></a></td><td>{stage_chip(st)}</td><td style="text-align:left">{e(st["sub"])}</td>'
                      f"<td>{trend_txt(st)}</td><td>{pc(st['to50'])}</td><td>{pc(st['to200'])}</td><td>{pc(st['sl200'], 2)}</td>"
                      f"<td>{pc(st['from_hi'])}</td></tr>")
    B.append(f'<div class="card tw"><table><tr><th>代号</th><th>阶段</th><th style="text-align:left">状态</th><th>趋势 中期/长期</th>'
             f"<th>距50天线</th><th>距200天线</th><th>200天线月斜率</th><th>距52周高</th></tr>{srows}</table>"
             '<div class="sub" style="margin-top:6px">'
             "<b>Stage 2</b>：收盘 &gt; 200 天线且 200 天线向上（近 21 日 ≥ +1%）；50 天线未上穿 200 天线时标「早期」。"
             "<b>Stage 4</b>：收盘 &lt; 200 天线且 200 天线向下（近 21 日 ≤ −1.5%）；50 天线未下穿时标「刚转入」。"
             "<b>Stage 1 / 3</b>：200 天线走平或信号未一致，之前 6 个月 200 天线在升 → Stage 3（做顶），在跌 → Stage 1（筑底）。"
             "<br><b>趋势</b>：中期 = 收盘相对 50 天线 + 50 天线方向（上升 / 下跌 / 震荡）；长期 = 200 天线方向。"
             "均线由 Yahoo 日线计算，并与 TradingView 的 50 / 200 天线核对（误差 1% 内）。"
             "程序判断只是近似，转折期（Stage 1↔2、3↔4）请看图确认。</div></div>")

    # 3. 大涨大跌原因
    bigs = [expl[s] for s in order if expl.get(s) and expl[s]["big"]]
    B.append(f"<h2>3. 为何大涨大跌（{mv_col}超过 ±{th:g}%）</h2>")
    if not bigs:
        B.append(f'<div class="card mut">今天没有涨跌超过 ±{th:g}% 的股票。</div>')
    def line(l):
        if "link" not in l:
            return f"<li>{e(l['text'])}</li>"
        return (f'<li><span class="chip">{e(l["tags"])}</span><a href="{e(l["link"])}" target="_blank" rel="noopener">{e(l["text"])}</a>'
                f'<br><span class="mut">🕒 {when_txt(l["when"])} · {e(l["src"])}</span>' + (f'<br><span class="mut">{e(l["desc"])}</span>' if l["desc"] else "") + "</li>")

    for x in bigs:
        B.append(f'<div class="card"><span class="sym">{x["sym"]}</span> {pc(x["move"])} '
                 f'<span class="mut">{e(quotes[x["sym"]].get("description"))}</span><ul>'
                 + "".join(line(l) for l in x["lines"])
                 + (f'<li>📉📈 {e(x["ctx"])}</li>' if x["ctx"] else "") + "</ul></div>")

    # 4. 新闻
    B.append("<h2>4. 新闻摘要</h2>")
    for s in order:
        q = quotes[s]
        ns = news_map.get(s, [])
        x = expl.get(s) or {}
        tags = "".join(f'<span class="chip">{t}</span>' for t, _ in reasons_from(ns)[:4])
        er = "".join(f'<span class="chip">📅 {e(n)}</span>' for n in earnings_note(q, dt.datetime.now(dt.timezone.utc)))
        items = ""
        for it in ns:
            zt, zd = it.get("zh_title"), it.get("zh_desc")
            items += (f'<li><a href="{e(it["link"])}" target="_blank" rel="noopener">{e(zt or it["title"])}</a>'
                      f'<br><span class="mut">🕒 {when_txt(it["when"])} · {e(it["src"])}</span>'
                      + (f'<br><span>{e(zd)}</span>' if zd else (f'<br><span class="en">{e(it["desc"][:260])}</span>' if it["desc"] else ""))
                      + (f'<br><span class="en">{e(it["title"])}</span>' if zt else "") + "</li>")
        stale = " <span class=\"mut\">（24 小时内没有新闻，已放宽到 48 小时）</span>" if ns and ns[0].get("wide") else ""
        st = checks.get(s, {}).get("stage")
        sline = (f'<div style="margin:4px 0">{stage_chip(st)} {e(st["sub"])} · 趋势 {trend_txt(st)}'
                 f'<br><span class="mut">判断依据：{e(st["why"])}；收盘 {price(st["close"])}，50 天线 {price(st["s50"])}（{st["to50"]:+.1f}%），'
                 f'200 天线 {price(st["s200"])}（{st["to200"]:+.1f}%），距 52 周高 {st["from_hi"]:+.1f}%</span></div>') if st else ""
        B.append(f'<div class="card" id="{s}"><span class="sym">{s}</span> {pc(move_of(q, mode))} '
                 f'<span class="mut">{e(q.get("description"))} · {e(q.get("industry") or "")}</span>{stale}{sline}{er}{tags}'
                 + (f"<ul>{items}</ul>" if items else '<div class="mut">最近 48 小时没有找到相关新闻。</div>') + "</div>")

    B.append('<div class="foot">数据：TradingView（价格可能延迟约 15 分钟）、Yahoo Finance 与 Google News RSS。'
             "中文为机器翻译，以原文为准；「原因」按新闻关键词和大盘对比自动归类，是推断，不保证正确。"
             "<br>仅供学习，不构成投资建议。</div>")
    return page(f"观察清单 {label} {now_et:%Y-%m-%d}", "".join(B))


SITE = "https://yeohkj-dev.github.io/daily-stock-brief/"
NL = chr(10)


def telegram_text(mode, et_now, syms, quotes, bench, expl, checks, news_chk, cfg):
    """Telegram 简报（HTML 格式，4096 字以内）。"""
    T = lambda x: html.escape(str(x), quote=False)
    p = lambda x: "—" if x is None else f"{x:+.1f}%"
    W = "一二三四五六日"
    label = "盘前版" if mode == "premarket" else "收盘版"
    th = cfg["big_move_pct"]
    L = [f"<b>📈 观察清单 · {label}</b>",
         f"{et_now:%Y-%m-%d}（周{W[et_now.weekday()]}）美东 {et_now:%H:%M} · 大马 {et_now.astimezone(MYT):%m-%d %H:%M}", ""]
    L.append("<b>大盘</b> " + " · ".join(f"{s} {p(move_of(bench[s], mode))}" for s, _ in BENCH if s in bench))
    mvs = [(s, move_of(quotes[s], mode)) for s in syms if s in quotes and move_of(quotes[s], mode) is not None]
    if mvs:
        L.append(f"<b>清单</b> 涨 {sum(m > 0 for _, m in mvs)} · 跌 {sum(m < 0 for _, m in mvs)} · 平均 {p(sum(m for _, m in mvs) / len(mvs))}")
    nf = sum(checks.get(s, {}).get("level") != "ok" for s in syms)
    L.append("🔎 数据核对：" + ("✅ 全部与 Yahoo 一致" if nf == 0 else f"⚠ {nf} 只有差异，详见网页"))

    order = sorted(mvs, key=lambda x: x[1])
    big = [(s, m) for s, m in order if abs(m) >= th]
    L += ["", f"<b>🔥 大涨大跌（{'盘前' if mode == 'premarket' else '今日'} ±{th:g}%）：{len(big)} 只</b>"]
    for s, m in sorted(big, key=lambda x: -abs(x[1])):
        st = (checks.get(s) or {}).get("stage")
        x = expl.get(s) or {}
        news = [l for l in x.get("lines", []) if "link" in l]
        er = [l for l in x.get("lines", []) if "link" not in l and l["text"].startswith("📅")]
        line = f"• <b>{T(s)}</b> {p(m)}" + (f" · Stage {st['stage']}" if st else "")
        if news:
            n = news[0]
            t = n["text"] if len(n["text"]) <= 60 else n["text"][:58] + "…"
            line += NL + f"   {T(t)}（🕒 {to_et(n['when']):%m-%d %H:%M} 美东）"
        elif er:
            line += NL + f"   {T(er[0]['text'])}"
        else:
            line += NL + "   没找到明确个股消息"
        if x.get("ctx"):
            line += NL + f"   <i>{T(x['ctx'].split('；')[0])}</i>"
        L.append(line)
    if not big:
        L.append("没有")

    L += ["", "<b>📊 阶段</b>"]
    for k in (2, 1, 3, 4):
        ss = [s for s in syms if ((checks.get(s) or {}).get("stage") or {}).get("stage") == k]
        if ss:
            L.append(f"{STAGE_NAME[k]}：" + "、".join(ss))
    up = [s for s in syms if ((checks.get(s) or {}).get("stage") or {}).get("mid") == "上升"]
    if up:
        L.append("中期上升趋势：" + "、".join(up))
    L += ["", f'<a href="{SITE}">👉 完整报告（新闻、原因、日期链接）</a>', "<i>仅供学习，不构成投资建议</i>"]
    text = NL.join(L)
    if len(text) > 4000:                 # Telegram 上限 4096 字：太长就去掉每只股票的新闻细节行
        L = [l.split(NL)[0] if l.startswith("• ") else l for l in L]
        text = NL.join(L)
    return text[:4090]


def write_archive_index(keep):
    files = sorted((f for f in os.listdir(ARCH) if f.endswith(".html") and f != "index.html"), reverse=True)
    for old in files[keep:]:
        os.remove(os.path.join(ARCH, old))
    files = files[:keep]
    li = "".join(f'<li><a href="{f}">{f[:10]} · {"盘前版" if "premarket" in f else "收盘版"}</a></li>' for f in files)
    body = f'<div class="nav"><a href="../">最新报告</a></div><h1>历史报告</h1><div class="card"><ul>{li}</ul></div>'
    open(os.path.join(ARCH, "index.html"), "w", encoding="utf-8").write(page("观察清单 历史报告", body))


# ---------------- 主程序 ----------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    force = "--force" in argv
    mode = argv[argv.index("--mode") + 1] if "--mode" in argv else None
    now = dt.datetime.now(dt.timezone.utc)
    et = to_et(now)
    hm = (et.hour, et.minute)
    if mode is None:
        mode = "premarket" if (4, 0) <= hm <= (9, 29) else "close" if (16, 0) <= hm <= (20, 59) else None
        if mode is None and force:
            mode = "premarket" if hm < (16, 0) else "close"
    if mode is None:
        print(f"美东现在 {et:%a %H:%M}，不在报告时间（盘前 04:00–09:29，收盘 16:00–20:59），跳过")
        return
    if not force and (et.weekday() >= 5 or str(et.date()) in HOLIDAYS):
        print(f"{et.date()} 美股休市，跳过")
        return
    name = f"{et.date()}-{mode}.html"
    os.makedirs(ARCH, exist_ok=True)
    if not force and os.path.exists(os.path.join(ARCH, name)):
        print(f"今天已经出过 {name}，跳过")
        return

    cfg = read_config()
    syms = read_watchlist()
    print(f"模式 {mode}，{len(syms)} 只股票：{' '.join(syms)}")
    quotes = tv_quotes(syms)
    bench = tv_quotes([s for s, _ in BENCH])
    missing = [s for s in syms if s not in quotes]
    if missing:
        print("TradingView 找不到：", missing)
    if len(missing) > len(syms) / 2:
        raise SystemExit("超过一半股票取不到价格，数据源可能有问题，这次不发布")

    checks, want = verify(syms + [s for s, _ in BENCH], {**quotes, **bench}, mode, et)
    for s, c in checks.items():
        if c["level"] != "ok":
            print(f"核对 {s} [{c['level']}]：{'；'.join(c['notes'])}")
    nfail = sum(checks[s]["level"] == "fail" for s in syms)
    print(f"核对：{len(syms) - nfail}/{len(syms)} 只通过（对照交易日 {want}）")
    if nfail > len(syms) * 0.3 and not force:
        raise SystemExit(f"{nfail}/{len(syms)} 只股票价格核对不通过，这次不发布（网站保留上一份）")

    hours = cfg["news_hours"]

    def one(s):
        names = names_for(s, quotes.get(s))
        allnews = get_news(s, names, now - dt.timedelta(hours=hours * 2))
        recent = [x for x in allnews if x["when"] >= now - dt.timedelta(hours=hours)]
        picked = rank(recent or allnews, cfg["news_per_stock"])
        if not recent and picked:
            picked[0]["wide"] = True
        return s, picked

    with ThreadPoolExecutor(6) as ex:
        news_map = dict(ex.map(one, [s for s in syms if s in quotes]))

    if cfg["translate"]:
        texts = [t for ns in news_map.values() for it in ns for t in (it["title"], it["desc"][:400])]
        zh = translate(texts)
        for ns in news_map.values():
            for it in ns:
                it["zh_title"] = zh.get(it["title"])
                it["zh_desc"] = zh.get(it["desc"][:400]) if it["desc"] else None
        print(f"翻译 {len(zh)}/{len(set(t for t in texts if t))} 条")

    expl = {s: explain(s, quotes[s], mode, bench, news_map.get(s, []), cfg["big_move_pct"], now) for s in syms if s in quotes}
    # 新闻核对：时间窗口、是否点名该股、翻译覆盖
    nn = [it for ns in news_map.values() for it in ns]
    news_chk = dict(total=len(nn), stocks_without=[s for s in syms if s in quotes and not news_map.get(s)],
                    oldest_h=max([(now - it["when"]).total_seconds() / 3600 for it in nn] or [0]),
                    in_title=sum(it["in_title"] for it in nn),
                    translated=sum(bool(it.get("zh_title")) for it in nn) if cfg["translate"] else None)
    out = build_html(mode, et, syms, quotes, bench, news_map, expl, cfg, missing, checks, want, news_chk)
    open(os.path.join(ARCH, name), "w", encoding="utf-8").write(out.replace('href="./"', 'href="../"').replace('href="archive/"', 'href="./"'))
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(out)
    open(os.path.join(DOCS, ".nojekyll"), "w").close()
    write_archive_index(int(cfg["keep_archive"]))
    if cfg["telegram"] in (mode, "both"):
        tg = telegram_text(mode, et, syms, quotes, bench, expl, checks, news_chk, cfg)
        os.makedirs(os.path.join(ROOT, "telegram"), exist_ok=True)
        with open(os.path.join(ROOT, "telegram", f"{et.date()}-{mode}.json"), "w", encoding="utf-8") as f:
            json.dump(dict(id=f"{et.date()}-{mode}", telegram_html=tg), f, ensure_ascii=False, indent=1)
        print(f"Telegram 简报已准备（{len(tg)} 字）")
    big = [f"{x['sym']} {x['move']:+.1f}%" for x in expl.values() if x and x["big"]]
    print(f"完成：docs/index.html、docs/archive/{name}；大涨大跌：{', '.join(big) or '无'}")


if __name__ == "__main__":
    main()
