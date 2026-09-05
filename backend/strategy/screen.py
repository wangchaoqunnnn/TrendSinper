# -*- coding: utf-8 -*-
"""选股流程（screen）：实时最新一次信号日执行“选股”。

流程：快照硬过滤 → 限量拉取日K（并行） → 行业/大盘上下文 → 全维度打分排序
→ 板块均衡 → 门槛不足自动放宽 → 生成推荐理由与止损价。
"""
import random

from .. import config
from . import engine


def _meta_ok(meta, p):
    """硬性流动性与标的过滤（对应策略第三步前的可交易性约束）。"""
    sc = p["screen"]
    if meta["is_st"] and sc["exclude_st"]:
        return False
    price = meta["price"]
    if price is None or price <= 0:
        return False
    if not (sc["min_price"] <= price <= sc["max_price"]):
        return False
    fmv = meta.get("float_mv")
    if fmv:
        if not (sc["min_float_mv"] <= fmv / 1e8 <= sc["max_float_mv"]):
            return False
    # 排除当日已接近涨停（次日难以按信号价买入）与跌停/停牌
    if sc.get("exclude_nearly_limit_up", True):
        lim = config.limit_up_pct(meta["board"])
        pct = (meta.get("pct") or 0.0) / 100.0
        if pct >= lim * 0.95 or pct <= -lim * 0.95:
            return False
    if meta.get("name") and (meta["name"].startswith("N") or meta["name"].startswith("C")):
        return False  # 新股/次新上市初期无足够历史
    return True


def _prefilter(meta_list, p, max_fetch=900, per_industry=30):
    """按可交易性粗筛，并按行业限量抽样（兼顾板块宽度与拉取预算）。"""
    keep = [m for m in meta_list if _meta_ok(m, p)]
    by_ind = {}
    for m in keep:
        by_ind.setdefault(m["industry"], []).append(m)
    chosen = []
    inds = sorted(by_ind.keys())
    # 确定性打散，避免行业排序偏差
    rnd = random.Random(20240905)
    rnd.shuffle(inds)
    for ind in inds:
        members = by_ind[ind]
        rnd.shuffle(members)
        chosen.extend(members[:per_industry])
    rnd.shuffle(chosen)
    return chosen[:max_fetch]


def compute_stop(entry_price, arr, t, params):
    """止损价 = max(MA20×(1-2%), 近10日回调低点×(1-2%), 买入价×(1-止损%))。

    取三者最高者：趋势未破坏就持有（顺势不动摇），关键支撑破位坚决离场（破位坚决跑）。
    """
    rk = params["risk"]
    floor_pct = rk.get("swing_floor_pct", 0.02)
    ma20 = arr["ma20"][t]
    stop_ma = (ma20 * (1 - floor_pct)) if ma20 else 0.0
    seg = max(1, t - 10)
    swing = min(arr["l"][seg:t + 1]) if t >= seg else arr["l"][t]
    stop_swing = swing * (1 - floor_pct)
    stop_hard = entry_price * (1 - rk["stop_pct"])
    stop = max(stop_ma, stop_swing, stop_hard)
    if stop <= 0:
        stop = stop_hard
    if stop == stop_ma and stop_ma > 0:
        why = "跌破MA20关键支撑（%.2f 的 98%% 分位防线）" % ma20
    elif stop == stop_swing:
        why = "跌破近10日回调低点支撑 %.2f（上涨结构破坏）" % swing
    else:
        why = "固定比例止损（买入价回撤 %.0f%%）" % (rk["stop_pct"] * 100)
    return round(stop, 2), why


def _board_cap(p):
    sc = p["screen"]
    return max(1, int(round(sc["top_n"] * sc.get("max_per_board_ratio", 0.5))))


def pick_payload(code, meta, arr, t, ctx, params, ev):
    """组装最终推荐对象（含理由、止损价、风险提示）。"""
    sc = params["screen"]
    entry_price = round(arr["c"][t], 2)
    stop_price, stop_why = compute_stop(entry_price, arr, t, params)
    ma20 = arr["ma20"][t]
    ret21 = arr["c"][t] / arr["c"][t - 21] - 1.0 if t >= 21 else None
    fund = (ctx.funds.get(meta["code"]) or {}) if ctx.funds else {}
    return {
        "code": meta["code"], "name": meta["name"], "board": meta["board"],
        "industry": meta["industry"], "price": round(meta["price"], 2),
        "entry_price": entry_price, "entry_date": arr["dates"][t],
        "score": ev["score"], "new_high": ev["new_high"],
        "ma20": round(ma20, 2) if ma20 else None,
        "pct_1m": round(ret21 * 100, 2) if ret21 is not None else None,
        "reasons": ev["reasons"], "detail": ev["detail"],
        "stop_price": stop_price, "stop_why": stop_why,
        "risk_notes": [
            "若收盘跌破 %.2f（%s）坚决止损离场" % (stop_price, stop_why),
            "趋势票不预测顶：破位就走，避免把趋势票拿成价值投资",
        ],
        "fund": fund,
        "float_mv": round((meta.get("float_mv") or 0) / 1e8, 2),
        "limit_pct": config.limit_up_pct(meta["board"]),
    }


def screen(provider, params=None, progress=None, top_n=None):
    """执行一次“今日选股”。progress(msg, frac) 可选。返回完整结果 dict。"""
    params = params or config.load_params()
    sc = params["screen"]

    def log(msg, frac=None):
        if progress:
            progress(msg, frac)

    log("拉取全市场快照…", 0.02)
    meta_list = provider.snapshot()
    meta_by_code = {m["code"]: m for m in meta_list}

    log("硬过滤并限量候选（预算 %d 只）…" % sc.get("max_kline_fetch", 900), 0.08)
    cands = _prefilter(meta_list, params, max_fetch=sc.get("max_kline_fetch", 900),
                       per_industry=sc.get("per_industry", 30))

    # ---- 并行拉K线 ----
    log("并行拉取 %d 只候选日K…" % len(cands), 0.12)
    total = len(cands)
    done = [0]

    def load(m):
        bars = provider.bars(m["code"])
        done[0] += 1
        if progress and done[0] % 50 == 0:
            progress("拉取K线 %d/%d" % (done[0], total), 0.12 + 0.5 * done[0] / max(total, 1))
        if len(bars) < sc["min_history_days"]:
            return None
        return engine.build_arrays(bars, shares_float=m.get("shares_float"))

    arrs = {}
    from .. import dataapi
    results = dataapi.parallel(cands, load, workers=10)
    for i, m in enumerate(cands):
        a = results[i]
        if a is not None:
            arrs[m["code"]] = a

    if not arrs:
        return {"picks": [], "meta": {"error": "无足够K线数据的候选（数据源异常？）"}}

    # 信号日 = 全市场共同的最新交易日；剔除长期停牌（最后K线早于大盘）的标的
    from collections import Counter
    last_dates = Counter(a["dates"][-1] for a in arrs.values())
    market_date = last_dates.most_common(1)[0][0]
    arrs = {code: a for code, a in arrs.items() if a["dates"][-1] == market_date}
    log("K线就绪 %d 只（信号日 %s）" % (len(arrs), market_date), 0.62)
    if not arrs:
        return {"picks": [], "meta": {"error": "无交易日对齐的候选"}}

    # ---- 上下文：大盘 + 行业景气 + 基本面 ----
    idx = len(next(iter(arrs.values()))["dates"]) - 1
    date = market_date
    log("构建行业景气与大盘上下文…", 0.68)
    bench = provider.index_bars()
    bench_closes = [b["c"] for b in bench]
    bench_pos = None
    for i in range(len(bench) - 1, -1, -1):
        if bench[i]["date"] <= date:
            bench_pos = i
            break

    sector = {}
    for code, a in arrs.items():
        m = meta_by_code.get(code)
        i = len(a["dates"]) - 1
        if m is None or i < 20 or a["ma60"][i] is None:
            continue
        ind = m["industry"]
        s = sector.setdefault(ind, {"members": 0, "above": 0, "rets": []})
        s["members"] += 1
        if a["c"][i] > a["ma60"][i]:
            s["above"] += 1
        s["rets"].append(a["c"][i] / a["c"][i - 20] - 1.0)
    sector_stats = {}
    for ind, s in sector.items():
        sector_stats[ind] = {
            "members": s["members"],
            "above_frac": s["above"] / s["members"],
            "ret20": sum(s["rets"]) / len(s["rets"]),
        }

    ctx = engine.EngineCtx()
    ctx.bench_ok = bench_pos is not None and bench_pos >= 21
    ctx.bench_closes = bench_closes
    ctx.bench_pos = bench_pos
    ctx.sector = sector_stats
    ctx.funds = provider.fundamentals()
    ctx.date = date

    # ---- 打分排序 ----
    log("全维度打分…", 0.8)
    scored = []
    for code, a in arrs.items():
        m = meta_by_code.get(code)
        i = len(a["dates"]) - 1          # 各自最后一个交易日（与信号日对齐）
        if i < 120:                      # 均线数据不足的次新股跳过
            continue
        ev = engine.evaluate(code, a, m, i, ctx, params, reasons=True)
        scored.append((ev["score"], code, ev, a, m, i))
    scored.sort(key=lambda x: x[0], reverse=True)

    # ---- 门槛 + 板块均衡 + 自动放宽 ----
    threshold = sc["min_score"]
    relaxed = False
    floor = max(35.0, sc["min_score"] - 25.0)
    while True:
        picks = []
        board_cnt = {}
        cap = _board_cap(params)
        for score, code, ev, a, m, i in scored:
            if score < threshold:
                break
            b = m["board"]
            if board_cnt.get(b, 0) >= cap:
                continue
            board_cnt[b] = board_cnt.get(b, 0) + 1
            picks.append((code, ev, a, m, i))
            if len(picks) >= sc["top_n"]:
                break
        if len(picks) >= 3 or threshold <= floor or not sc["auto_relax"]:
            break
        threshold = round(threshold - 5.0, 1)
        relaxed = True

    out_picks = []
    for code, ev, a, m, i in picks:
        out_picks.append(pick_payload(code, m, a, i, ctx, params, ev))

    meta_out = {
        "date": date, "provider": provider.mode, "provider_label": provider.label,
        "params_version": params.get("version"), "threshold": threshold,
        "relaxed": relaxed, "relaxed_note": ("未达原始门槛(%.0f分)，已自动放宽至 %.0f 分" % (
            sc["min_score"], threshold)) if relaxed else None,
        "counts": {
            "universe": len(meta_list), "candidate": len(cands),
            "with_kline": len(arrs), "picked": len(out_picks),
        },
    }
    return {"picks": out_picks, "meta": meta_out}
