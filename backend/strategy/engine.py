# -*- coding: utf-8 -*-
"""策略引擎：把《趋势策略.md》量化为可打分、可解释、可回测的规则。

  第一步 自上而下找赛道   → sector（行业景气度：板块内多只走强、板块趋势向上）
  第二步 基本面筛选       → fundamental（高ROE、业绩增长、机构认可）
  第三步 技术面确认       → ma_bull / new_high / pullback_rising / volume
                            / month_ret / turnover / relative_strength / anti_weak
  第四步 实战选出+风控     → 加权打分排序、止损价与破位规则（买卖看支撑，破位坚决跑）

说明：历史回测时基本面采用“当前最新可得”报告期数据（跨期对比有轻微前视偏差，
仅在报告中注明，不影响相对调优结论）；技术面规则完全基于截至当日的历史K线。
"""
from .. import indicators as ind


# ---------------------------------------------------------------------------
# 数组预计算（一次算好，回测/选股共用，避免重复开销）
# ---------------------------------------------------------------------------
def build_arrays(bars, shares_float=None):
    """bars: list[dict(date,o,h,l,c,v,amount)]（升序）→ 计算后的数组字典。"""
    n = len(bars)
    dates = [b["date"] for b in bars]
    o = [b["o"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    v = [b["v"] or 0.0 for b in bars]
    return build_arrays_lists(o, h, l, c, v, dates, shares_float)


def build_arrays_lists(o, h, l, c, v, dates, shares_float=None):
    """同 build_arrays，但直接接收序列（预热阶段复用，避免二次读库）。"""
    arr = {
        "dates": dates, "o": o, "h": h, "l": l, "c": c, "v": v,
        "ma20": ind.sma(c, 20), "ma60": ind.sma(c, 60), "ma120": ind.sma(c, 120),
        "rm60": ind.rolling_max(h, 60), "rm120": ind.rolling_max(h, 120),
        "rm250": ind.rolling_max(h, 250),
    }
    # 换手率序列（日，小数分式 0.02=2%）：成交量(手)*100股 / 流通股本
    arr["turn"] = None
    if shares_float and shares_float > 0:
        arr["turn"] = [v_i * 100.0 / shares_float for v_i in v]
    return arr


def _new_high_window(arr, t, windows=(60, 120, 250)):
    """在 t 日相对之前 windows 天是否创阶段新高；返回 (window, prev_high)。"""
    for w in sorted(windows):
        if t >= w and arr["h"][t] > arr["rm" + str(w)][t - 1]:
            return (w, arr["rm" + str(w)][t - 1])
    return (None, None)


# ---------------------------------------------------------------------------
# 逐项规则
# ---------------------------------------------------------------------------
def _ma_bull(arr, t, p):
    r = p["rules"]["ma_bull"]
    if t < 120 or arr["ma120"][t] is None:
        return False, None
    rising = arr["ma60"][t] > arr["ma60"][t - r["mid_rising_days"]]
    bull = (arr["c"][t] > arr["ma20"][t] > arr["ma60"][t] > arr["ma120"][t]) and rising
    detail = "价格 %.2f > MA20 %.2f > MA60 %.2f > MA120 %.2f，且MA60 上行" % (
        arr["c"][t], arr["ma20"][t], arr["ma60"][t], arr["ma120"][t])
    return bull, detail


def _sector(meta, sec_stats, p):
    r = p["rules"]["sector"]
    st = sec_stats or {}
    s = st.get(meta["industry"])
    if not s or s["members"] < r["min_members"]:
        return False, "行业样本不足，暂不纳入评分"
    above = s["above_frac"]
    ret20 = s["ret20"]
    ok = above >= r["min_frac_above_ma60"] and ret20 >= r["window_ret_min"]
    detail = "行业[%s] %d只样本中 %.0f%% 站上MA60，20日板块均涨 %+.2f%%（板块趋势向上）" % (
        meta["industry"], s["members"], above * 100, ret20 * 100)
    return ok, detail


def _fundamental(meta, funds, p):
    f = p["fundamental"]
    fund = funds.get(meta["code"]) if funds else None
    if not fund:
        return None, "（暂无业绩数据，基本面维度不参与评分）"
    roe = fund.get("roe")
    np_yoy = fund.get("np_yoy")
    rev_yoy = fund.get("rev_yoy")
    inst = fund.get("inst")
    have = [x is not None for x in (roe, np_yoy, rev_yoy)]
    if not any(have):
        return None, "（业绩字段缺失）"
    ok = True
    parts = []
    if roe is not None:
        ok &= roe >= f["min_roe"]
        parts.append("ROE %.1f%%" % roe)
    if np_yoy is not None:
        ok &= np_yoy >= f["min_np_yoy"]
        parts.append("归母净利同比 %+.1f%%" % np_yoy)
    if rev_yoy is not None:
        ok &= rev_yoy >= f["min_rev_yoy"]
        parts.append("营收同比 %+.1f%%" % rev_yoy)
    extra = ""
    if inst is not None and inst > 8:
        extra = "；机构持仓约 %.1f%%" % inst
    report = "（%s）" % (fund.get("report_date") or "")
    return bool(ok), "基本面：%s%s%s —— 高ROE/业绩持续增长/机构认可" % (
        "、".join(parts), extra, report)


def _month_ret(arr, t, p):
    r = p["rules"]["month_ret"]
    if t < r["days"]:
        return False, None
    ret = arr["c"][t] / arr["c"][t - r["days"]] - 1.0
    ok = r["min"] <= ret <= r["max"]
    if ret > r["max"]:
        return False, "近%d日涨幅 %+.1f%% 过热（>%d%%）" % (r["days"], ret * 100, r["max"] * 100)
    if ret < r["min"]:
        return False, "近%d日涨幅 %+.1f%%（<门槛%d%%）" % (r["days"], ret * 100, r["min"] * 100)
    return True, "近%d日涨幅 %+.1f%%（区间 %d%%~%d%% 稳健上行）" % (
        r["days"], ret * 100, r["min"] * 100, r["max"] * 100)


def _turnover(arr, t, p):
    r = p["rules"]["turnover"]
    if arr["turn"] is None or t < r["days"]:
        return False, "（无换手数据）"
    vals = arr["turn"][t - r["days"] + 1:t + 1]
    if not vals:
        return False, None
    avg = sum(vals) / len(vals)
    ok = r["min"] <= avg <= r["max"]
    return ok, "近%d日平均换手 %.2f%%（适中区间 %.1f%%~%.1f%%）" % (
        r["days"], avg * 100, r["min"] * 100, r["max"] * 100)


def _pullback_rising(arr, t, p):
    r = p["rules"]["pullback_rising"]
    rc, pr = r["recent"], r["prior"]
    if t < rc + pr:
        return False, None
    recent_min = min(arr["l"][t - rc:t])
    prior_min = min(arr["l"][t - rc - pr:t - rc])
    ok = recent_min > prior_min
    return ok, "回调低点抬高：近%d日低点 %.2f > 前段低点 %.2f" % (
        rc, recent_min, prior_min)


def _volume(arr, t, p):
    r = p["rules"]["volume"]
    base, upd = r["base_days"], r["up_avg_days"]
    if t < base:
        return False, None
    seg = t - base + 1
    base_avg = sum(arr["v"][seg - 1:t]) / base
    ups = [arr["v"][i] for i in range(t - upd + 1, t + 1) if arr["c"][i] > arr["c"][i - 1]]
    downs = [arr["v"][i] for i in range(seg, t + 1) if arr["c"][i] < arr["c"][i - 1]]
    if base_avg <= 0 or not ups or not downs:
        return False, None
    up_avg = sum(ups) / len(ups)
    dn_avg = sum(downs) / len(downs)
    mild_up = up_avg <= base_avg * r["mild_up_max"]
    shrink = dn_avg <= base_avg * r["shrink_down_max"]
    ok = mild_up and shrink
    return ok, "量价配合：上涨均量/基础量 %.2f（温和），回调均量/基础量 %.2f（缩量）" % (
        up_avg / base_avg, dn_avg / base_avg)


def _relative_strength(arr, t, ctx, p):
    r = p["rules"]["relative_strength"]
    d = r["days"]
    if t < d or not ctx.bench_ok:
        return False, "（无基准数据）"
    s_ret = arr["c"][t] / arr["c"][t - d] - 1.0
    b_ret = ctx.bench_closes[ctx.bench_pos] / ctx.bench_closes[ctx.bench_pos - d] - 1.0 \
        if ctx.bench_pos >= d else None
    if b_ret is None:
        return False, None
    ok = (s_ret - b_ret) >= r["min"]
    return ok, "相对强度：个股20日 %+.2f%% vs 沪深300 %+.2f%%（超额 %+.2f%%）" % (
        s_ret * 100, b_ret * 100, (s_ret - b_ret) * 100)


def _anti_weak(arr, t, ctx, p):
    r = p["rules"]["anti_weak"]
    w = r["days"]
    if t < w or not ctx.bench_ok or ctx.bench_pos is None:
        return False, "（无基准数据）"
    n_down = 0
    stock_down = 0
    for i in range(max(1, t - w + 1), t + 1):
        b_ret = ctx.bench_closes[ctx.bench_pos - (t - i)] / ctx.bench_closes[ctx.bench_pos - (t - i) - 1] - 1.0 \
            if ctx.bench_pos - (t - i) - 1 >= 0 else None
        if b_ret is None:
            continue
        if b_ret < r["down_th"]:
            n_down += 1
            if arr["c"][i] < arr["c"][i - 1]:
                stock_down += 1
    if n_down == 0:
        return True, "近%d日大盘无明显下跌日（抗跌属性中性通过）" % w
    ratio = stock_down / n_down
    ok = ratio <= r["max_down_ratio"]
    return ok, "抗跌：近%d日大盘下跌%d日中，本股仅%d日跟跌（占比 %.0f%%）" % (
        w, n_down, stock_down, ratio * 100)


# ---------------------------------------------------------------------------
# 评价入口
# ---------------------------------------------------------------------------
class EngineCtx:
    """一次评价的环境：基准指数、行业景气统计、基本面表。"""

    def __init__(self):
        self.bench_ok = False
        self.bench_closes = []
        self.bench_pos = None      # 基准数组中与当前日期对应的位置
        self.sector = {}           # {industry: {members, above_frac, ret20}}
        self.funds = {}
        self.date = None


def _eval_dim(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return False, None


def evaluate(code, arr, meta, t, ctx, params, reasons=True, windows=None):
    """在 t 日对个股进行全维度评价。

    返回 dict: {score, passes, new_high, reasons(list), detail(dict)}
    """
    wts = params["weights"]
    windows = windows or params["rules"]["new_high"]["windows"]
    res = {"code": code, "score": 0.0, "passes": {}, "reasons": [],
           "detail": {}, "new_high": None}

    def dim(key, fn, *args):
        ok, detail = _eval_dim(fn, *args)
        res["passes"][key] = bool(ok)
        if detail:
            res["detail"][key] = detail
        return ok

    # 行业景气（自上而下）
    dim("sector", _sector, meta, ctx.sector, params)
    # 基本面
    f_pass = _fundamental(meta, ctx.funds, params)
    if f_pass[0] is None:
        res["passes"]["fundamental"] = False
    else:
        res["passes"]["fundamental"] = bool(f_pass[0])
    if f_pass[1]:
        res["detail"]["fundamental"] = f_pass[1]
    # 技术面
    dim("ma_bull", _ma_bull, arr, t, params)
    dim("month_ret", _month_ret, arr, t, params)
    dim("turnover", _turnover, arr, t, params)
    dim("pullback_rising", _pullback_rising, arr, t, params)
    dim("volume", _volume, arr, t, params)
    dim("relative_strength", _relative_strength, arr, t, ctx, params)
    dim("anti_weak", _anti_weak, arr, t, ctx, params)

    w, prev_h = _new_high_window(arr, t, windows)
    res["new_high"] = w
    if w:
        res["passes"]["new_high"] = True
        res["detail"]["new_high"] = "创近%d日新高，突破前高 %.2f（主力做多意愿强）" % (w, prev_h)
    else:
        res["passes"]["new_high"] = False
        res["detail"]["new_high"] = "尚未创近%d日新高" % min(windows)

    total_w = 0.0
    acc = 0.0
    for key, wgt in wts.items():
        if wgt <= 0:
            continue
        total_w += wgt
        if res["passes"].get(key):
            acc += wgt
    # 创新高是“主升浪确认”信号，额外加成（分子分母同加，保证归一）
    bonus = 0.05 if w else 0.0
    total_w += bonus
    acc += bonus
    res["score"] = round(acc / total_w * 100.0, 1) if total_w else 0.0

    if reasons:
        # 按权重降序生成中文理由
        order = sorted(wts.keys(), key=lambda k: wts.get(k, 0), reverse=True)
        for key in order:
            if key == "new_high":
                continue
            if res["passes"].get(key) and key in res["detail"]:
                res["reasons"].append("✓ " + res["detail"][key])
        if w and "new_high" in res["detail"]:
            res["reasons"].append("✓ " + res["detail"]["new_high"])
        # 理由不足时补充“接近达标”的观察项
        if len(res["reasons"]) < 4:
            for key in order:
                if key == "new_high" or res["passes"].get(key):
                    continue
                if key in res["detail"] and len(res["reasons"]) < 6:
                    res["reasons"].append("△ " + res["detail"][key])
    return res
