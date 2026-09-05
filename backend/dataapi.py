# -*- coding: utf-8 -*-
"""行情数据访问（标准库 urllib，无第三方依赖）。

数据源策略（Python 客户端实测可达性）：
  全市场快照  —— 新浪财经 hs_a 节点分页（含沪深主板/创业板/科创板/北证，
                返回现价/换手/市值/PE/PB/量额）；行业归属由东财业绩报表接口补齐。
  个股/指数K线 —— 腾讯(前复权) 为主，新浪为辅，东方财富 push2his 为兜底。
  业绩报表    —— 东方财富数据中心（含 ROE / 净利同比 / 营收同比 / 行业名）。
  批量现价    —— 腾讯 qt.gtimg。
"""
import concurrent.futures as cf
import json
import re
import ssl
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "Chrome/126.0 Safari/537.36",
      "Referer": "https://quote.eastmoney.com/"}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def _http_text(url, timeout=10, retries=2, headers=None):
    last = None
    for i in range(retries + 1):
        try:
            hdr = dict(UA)
            if headers:
                hdr.update(headers)
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.35 * (i + 1))
    raise last


def _http_json(url, timeout=10, retries=2, headers=None):
    return json.loads(_http_text(url, timeout=timeout, retries=retries, headers=headers))


def _fnum(v):
    try:
        if v is None or v == "-" or v == "":
            return None
        return float(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1) 全市场快照（新浪财经 hs_a，覆盖沪深主板/创业板/科创板/北证）
# ---------------------------------------------------------------------------
_SINA_NODE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "Market_Center.getHQNodeData")


def _sina_page(node, page, num=100):
    q = urllib.parse.urlencode({
        "page": page, "num": num, "sort": "symbol", "asc": 1,
        "node": node, "symbol": "", "_s_r_a": "page"})
    txt = _http_text(_SINA_NODE + "?" + q, timeout=12,
                     headers={"User-Agent": UA["User-Agent"],
                              "Referer": "https://finance.sina.com.cn/"})
    txt = txt.strip()
    if txt.startswith("var"):          # JSONP 包装剥离
        m = re.search(r"\[.*\]", txt, re.S)
        txt = m.group(0) if m else txt
    return json.loads(txt or "[]")


def fetch_snapshot():
    """新浪全市场快照（并行翻页）→ list[dict]。市值单位统一为“元”。"""
    first = _sina_page("hs_a", 1)
    if not isinstance(first, list):
        raise RuntimeError("新浪快照返回异常")
    pages = [first]
    page_no = 1
    stop = False
    # 每次并行拉 10 页；某页不足 100 行即视为最后一页（按 symbol 升序排列无空洞）
    while not stop and page_no < 70:
        batch = list(range(page_no + 1, page_no + 11))
        res = parallel(batch, lambda pg: _sina_page("hs_a", pg), workers=8)
        for i, pg in enumerate(batch):
            data = res[i]
            if not isinstance(data, list):
                continue
            page_no = pg
            pages.append(data)
            if len(data) < 100:
                stop = True
                break
        if not stop:
            page_no = batch[-1]
        time.sleep(0.03)

    rows = []
    for chunk in pages:
        rows.extend(chunk)
    out = []
    seen = set()
    for d in rows:
        sym = str(d.get("symbol") or "")
        if len(sym) < 8:
            continue
        code = sym[2:]
        if code in seen:
            continue
        seen.add(code)
        price = _fnum(d.get("trade"))
        prev = _fnum(d.get("settlement"))
        mktcap_wan = _fnum(d.get("mktcap"))      # 万元
        nmc_wan = _fnum(d.get("nmc"))            # 流通市值（万元）
        if price is None or price <= 0:          # 停牌/无行情
            continue
        pct = _fnum(d.get("changepercent"))
        out.append({
            "code": code, "name": str(d.get("name") or code),
            "price": price,
            "pct": pct if pct is not None else (price / prev - 1.0) * 100 if prev else 0.0,
            "vol": _fnum(d.get("volume")),
            "amount": _fnum(d.get("amount")),
            "turnover": _fnum(d.get("turnoverratio")),
            "pe": _fnum(d.get("per")),
            "high": _fnum(d.get("high")), "low": _fnum(d.get("low")),
            "prev_close": prev,
            "open": _fnum(d.get("open")),
            "total_mv": (mktcap_wan * 1e4) if mktcap_wan else None,
            "float_mv": (nmc_wan * 1e4) if nmc_wan else None,
            "pb": _fnum(d.get("pb")),
            "industry": "其他", "main_inflow_pct": None, "main_inflow": None,
        })
    return out


# ---------------------------------------------------------------------------
# 2) 个股日K（腾讯前复权为主 → 新浪兜底）
# ---------------------------------------------------------------------------
def _tencent_kline(code, start, end, count=800):
    from . import config
    sym = config.tx_symbol(code)
    param = "%s,day,%s,%s,%d,qfq" % (sym, start, end, count)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + \
        urllib.parse.quote(param, safe=",")
    j = _http_json(url, timeout=10, retries=1,
                   headers={"User-Agent": UA["User-Agent"], "Referer": "https://gu.qq.com/"})
    data = (j or {}).get("data") or {}
    node = data.get(sym) or {}
    lines = node.get("qfqday") or node.get("day") or []
    return lines


def _sina_kline(code, datalen=800):
    from . import config
    ex, _ = config.classify_board(code)
    sym = ex + code
    q = urllib.parse.urlencode({
        "symbol": sym, "scale": 240, "ma": "no", "datalen": datalen})
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
           "CN_MarketDataService.getKLineData?" + q)
    txt = _http_text(url, timeout=10, retries=1,
                     headers={"User-Agent": UA["User-Agent"],
                              "Referer": "https://finance.sina.com.cn/"})
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def _parse_kline_rows(raw_lines):
    """raw_lines: [date, open, close, high, low, volume, ...]（腾讯顺序）"""
    rows = []
    for line in raw_lines:
        if isinstance(line, str):
            parts = line.split(",")
        else:
            parts = [str(x) for x in line]
        if len(parts) < 6:
            continue
        date = parts[0]
        o, c, h, l = _fnum(parts[1]), _fnum(parts[2]), _fnum(parts[3]), _fnum(parts[4])
        v, amt = _fnum(parts[5]), _fnum(parts[6]) if len(parts) > 6 else None
        if o is None or c is None or h is None or l is None:
            continue
        rows.append((date, o, h, l, c, v if v else 0.0, amt if amt else 0.0))
    return rows


def _sina_parse_kline_rows(objs):
    rows = []
    for o in objs or []:
        date = o.get("day")
        o_ = _fnum(o.get("open"))
        h = _fnum(o.get("high"))
        l = _fnum(o.get("low"))
        c = _fnum(o.get("close"))
        v = _fnum(o.get("volume"))
        if not date or o_ is None or c is None:
            continue
        rows.append((date, o_, h, l, c, (v or 0) / 100.0, 0.0))  # 新浪量为股→手
    return rows


def fetch_bars(code, beg="20240101", end="20991231", count=800):
    """返回 list[(date, open, high, low, close, volume(手), amount)] 升序。"""
    start = "%s-%s-%s" % (beg[:4], beg[4:6], beg[6:8])
    end_d = "%s-%s-%s" % (end[:4], end[4:6], end[6:8]) if len(end) == 8 else end
    # 主源：腾讯前复权
    try:
        lines = _tencent_kline(code, start, end_d, count)
        rows = _parse_kline_rows(lines)
        if rows:
            return rows
    except Exception:  # noqa: BLE001
        pass
    # 备用：新浪日K（不复权）
    try:
        objs = _sina_kline(code, datalen=count)
        rows = _sina_parse_kline_rows(objs)
        if rows:
            return rows
    except Exception:  # noqa: BLE001
        pass
    # 兜底：东方财富 push2his（可能被风控，仅静默尝试一次）
    try:
        from . import config
        q = urllib.parse.urlencode({
            "secid": config.em_secid(code), "klt": 101, "fqt": 1,
            "beg": beg, "end": "20991231",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"})
        j = _http_json("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + q,
                       retries=0)
        klines = ((j or {}).get("data") or {}).get("klines") or []
        return _parse_kline_rows(klines)
    except Exception:  # noqa: BLE001
        return []


def fetch_index_bars(code="000300", beg="20220101", end="20991231", count=900):
    """指数日K（新浪主源，腾讯/东财兜底）。返回 (date,o,h,l,c,v,amount) 升序。"""
    from . import config
    ex = "sh" if code.startswith(("000", "399")) else "sz"
    sym = ex + code
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService."
           "getKLineData?symbol=" + sym + "&scale=240&ma=no&datalen=" + str(count))
    try:
        txt = _http_text(url, timeout=10, retries=1,
                         headers={"User-Agent": UA["User-Agent"],
                                  "Referer": "https://finance.sina.com.cn/"})
        m = re.search(r"\[.*\]", txt, re.S)
        if m:
            rows = _sina_parse_kline_rows(json.loads(m.group(0)))
            if rows:
                return rows
    except Exception:  # noqa: BLE001
        pass
    return _em_index(code)


def _em_index(code):
    try:
        q = urllib.parse.urlencode({
            "secid": "1." + code, "klt": 101, "fqt": 1, "beg": "20220101",
            "end": "20991231", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"})
        j = _http_json("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + q, retries=0)
        klines = ((j or {}).get("data") or {}).get("klines") or []
        out = []
        for line in klines:
            p = line.split(",")
            if len(p) >= 6:
                out.append((p[0], _fnum(p[1]), _fnum(p[3]), _fnum(p[4]), _fnum(p[2]),
                            _fnum(p[5]), _fnum(p[6]) if len(p) > 6 else 0.0))
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 3) 业绩报表（最新报告期全市场 ROE / 净利同比 / 营收同比 / 行业）
# ---------------------------------------------------------------------------
def _latest_report_date():
    q = urllib.parse.urlencode({
        "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
        "pageNumber": 1, "pageSize": 1,
        "sortColumns": "REPORTDATE", "sortTypes": -1,
        "filter": '(ISNEW="1")'})
    j = _http_json("https://datacenter-web.eastmoney.com/api/data/v1/get?" + q)
    data = ((j or {}).get("result") or {}).get("data") or []
    if data:
        return (data[0].get("REPORTDATE") or "")[:10]
    return None


def _page_earnings(report_date, pn=1):
    flt = urllib.parse.quote(f'(REPORTDATE=\'{report_date}\')')
    q = ("reportName=RPT_LICO_FN_CPD&columns=ALL&pageNumber=%d&pageSize=500&"
         "sortColumns=SECURITY_CODE&sortTypes=1&filter=%s" % (pn, flt))
    return _http_json("https://datacenter-web.eastmoney.com/api/data/v1/get?" + q)


def fetch_earnings():
    """抓取最新报告期全市场业绩（分页并行）。返回 (report_date, list[dict])。"""
    rd = _latest_report_date()
    if not rd:
        return None, []
    first = _page_earnings(rd, 1)
    result0 = (first or {}).get("result") or {}
    pages = result0.get("pages", 1)
    datas = {1: result0.get("data") or []}

    rest = list(range(2, pages + 1))
    res = parallel(rest, lambda pn: _page_earnings(rd, pn), workers=4)
    for i, pn in enumerate(rest):
        r = res[i]
        if r:
            datas[pn] = ((r or {}).get("result") or {}).get("data") or []

    out = []
    for pn in sorted(datas.keys()):
        for d in datas[pn]:
            out.append({
                "code": str(d.get("SECURITY_CODE")),
                "report_date": (d.get("REPORTDATE") or "")[:10],
                "roe": _fnum(d.get("WEIGHTAVG_ROE")),
                "np_yoy": _fnum(d.get("SJLTZ")),
                "rev_yoy": _fnum(d.get("YSTZ")),
                "eps": _fnum(d.get("BASIC_EPS")),
                "industry": d.get("BOARD_NAME") or None,
            })
    return rd, out


# ---------------------------------------------------------------------------
# 4) 腾讯批量现价（用于跟踪票盘中状态）
# ---------------------------------------------------------------------------
def fetch_quotes(codes):
    """codes: list[code] -> dict[code] = {price, prev_close, pct, ...}"""
    from . import config
    out = {}
    if not codes:
        return out
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        syms = ",".join(config.tx_symbol(c) for c in chunk)
        try:
            req = urllib.request.Request("https://qt.gtimg.cn/q=" + syms, headers=UA)
            with urllib.request.urlopen(req, timeout=8, context=CTX) as r:
                raw = r.read().decode("gbk", errors="replace")
            for line in raw.split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                sym, body = line.split("=", 1)
                sym_part = sym.split("_")[-1].strip('"v ')
                # 形如 sz002832 / bj920571 / sh600000 → 去掉交易所前缀得 6 位代码
                code = sym_part[2:] if len(sym_part) == 8 and sym_part[:2].isalpha() else sym_part
                body = body.strip("\"")
                parts = body.split("~")
                if len(parts) < 35:
                    continue
                price = _fnum(parts[3])
                prev = _fnum(parts[4])
                if price is None or prev is None or prev == 0:
                    continue
                out[code] = {
                    "name": parts[1], "price": price, "prev_close": prev,
                    "pct": (price / prev - 1.0) if prev else 0.0,
                    "high": _fnum(parts[33]), "low": _fnum(parts[34]),
                    "open": _fnum(parts[5]),
                    "ts": parts[30] if len(parts) > 30 else "",
                }
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.02)
    return out


# ---------------------------------------------------------------------------
# 并发小工具
# ---------------------------------------------------------------------------
def parallel(items, fn, workers=8):
    """对 items 并发执行 fn(item)，返回与 items 同序的结果列表；异常项为 None。"""
    res = [None] * len(items)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        fut = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for f in cf.as_completed(fut):
            i = fut[f]
            try:
                res[i] = f.result()
            except Exception:  # noqa: BLE001
                res[i] = None
    return res
