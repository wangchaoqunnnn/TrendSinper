# -*- coding: utf-8 -*-
"""选股流程（screen）：对全市场（沪主板/深主板/创业板/科创板/北证）全量扫描。

流程：
  1) 全市场快照（东方财富分交易所全量 5900+ 只，含停牌标的）
  2) 可交易性硬过滤（价格/市值/ST/涨跌停/停牌）→ 候选（默认全量参与，不抽样）
  3) 并行预热并增量更新日K（SQLite 缓存，只补缺失交易日）
  4) 计算行业景气（板块内站上MA60比例与20日涨幅）
  5) 全维度打分（两遍式：先算分排序，再为入选票生成完整理由，内存占用低）
  6) 板块保底 + 单板块上限 + 门槛自动放宽 → 输出推荐（理由 + 止损价）
"""
from array import array
from collections import Counter
import random

from .. import config
from . import engine


def empty_coverage():
    return {b: {"universe": 0, "suspended": 0, "candidate": 0,
                "with_kline": 0, "passed": 0, "picked": 0}
            for b in config.BOARDS}


def _meta_ok(meta, p):
    """硬性可交易性过滤。"""
    sc = p["screen"]
    if meta.get("suspended"):
        return False
    if meta["is_st"] and sc["exclude_st"]:
        return False
    price = meta["price"]
    if price is None or price <= 0:
        return False
    if not (sc["min_price"] <= price <= sc["max_price"]):
        return False
    fmv = meta.get("float_mv")
    if fmv:
        lo = sc.get("min_float_mv_by_board", {}).get(meta["board"], sc["min_float_mv"])
        hi = sc.get("max_float_mv_by_board", {}).get(meta["board"], sc["max_float_mv"])
        if not (lo <= fmv / 1e8 <= hi):
            return False
    if sc.get("exclude_nearly_limit_up", True):
        lim = config.limit_up_pct(meta["board"])
        pct = (meta.get("pct") or 0.0) / 100.0
        if pct >= lim * 0.95 or pct <= -lim * 0.95:
            return False
    name = meta.get("name") or ""
    if name.startswith("N") or name.startswith("C"):
        return False      # 新股/次新上市初期无足够历史
    return True


def _prefilter(meta_list, p):
    """返回全部通过硬过滤的候选（默认不设上限；配置了上限时才按行业均匀取样）。"""
    sc = p["screen"]
    keep = [m for m in meta_list if _meta_ok(m, p)]
    max_fetch = int(sc.get("max_kline_fetch", 0) or 0)
    if max_fetch and len(keep) > max_fetch:
        per_industry = int(sc.get("per_industry", 30) or 30)
        by_ind = {}
        for m in keep:
            by_ind.setdefault(m["industry"], []).append(m)
        rnd = random.Random(20240905)
        inds = sorted(by_ind.keys())
        rnd.shuffle(inds)
        chosen = []
        for ind in inds:
            members = by_ind[ind]
            rnd.shuffle(members)
            chosen.extend(members[:per_industry])
        rnd.shuffle(chosen)
        return chosen[:max_fetch]
    return keep


def compute_stop(entry_price, arr, t, params):
    """止损价 = max(MA20×(1-2%), 近10日回调低点×(1-2%), 买入价×(1-止损%))。"""
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


def _select(scored, params, threshold, top_n):
    """板块保底 + 单板块上限的选择。scored: [(score, code, meta, ev)] 降序。"""
    cap = _board_cap(params)
    picks = []
    board_cnt = Counter()
    covered = set()
    if params["screen"].get("ensure_board_coverage", True):
        for score, code, meta, ev in scored:
            if score < threshold:
                break
            b = meta["board"]
            if b in covered or board_cnt[b] >= cap:
                continue
            picks.append((score, code, meta, ev))
            board_cnt[b] += 1
            covered.add(b)
            if len(picks) >= top_n:
                return picks
    chosen_codes = {p[1] for p in picks}
    for score, code, meta, ev in scored:
        if len(picks) >= top_n:
            break
        if score < threshold or code in chosen_codes:
            continue
        b = meta["board"]
        if board_cnt[b] >= cap:
            continue
        picks.append((score, code, meta, ev))
        board_cnt[b] += 1
        chosen_codes.add(code)
    return picks


def screen(provider, params=None, progress=None, top_n=None):
    """执行一次“今日选股”（全市场全量）。返回 {picks, meta, coverage}。"""
    params = params or config.load_params()
    sc = dict(params["screen"])
    if top_n:
        sc["top_n"] = top_n
    params = dict(params)
    params["screen"] = sc

    def log(msg, frac=None):
        if progress:
            progress(msg, frac)

    # ---- 1. 全市场快照 ----
    log("拉取全市场快照（沪主板/深主板/创业板/科创板/北证）…", 0.02)
    meta_list = provider.snapshot()
    meta_by_code = {m["code"]: m for m in meta_list}
    coverage = empty_coverage()
    for m in meta_list:
        c = coverage.setdefault(m["board"], {"universe": 0, "suspended": 0, "candidate": 0,
                                             "with_kline": 0, "passed": 0, "picked": 0})
        c["universe"] += 1
        if m.get("suspended"):
            c["suspended"] += 1

    # ---- 2. 全量候选（默认不抽样） ----
    cands = _prefilter(meta_list, params)
    for m in cands:
        coverage[m["board"]]["candidate"] += 1
    log("全市场 %d 只 → 可交易候选 %d 只（全量参与）" % (len(meta_list), len(cands)), 0.08)

    # ---- 3. 先用批量行情补齐“当日K线”（60只/请求，快速），再逐只兜底 ----
    bench = provider.index_bars()
    date = bench[-1]["date"] if bench else ""
    log("批量同步当日K线（腾讯批量行情，%d 只）…" % len(cands), 0.09)
    try:
        provider.sync_today_bars([m["code"] for m in cands], date, progress=progress)
    except Exception:  # noqa: BLE001
        pass

    from .. import dataapi
    total = len(cands)
    done = [0]
    workers = int(sc.get("fetch_workers", 12) or 12)

    def preload(m):
        bars = provider.bars(m["code"])
        done[0] += 1
        if progress and done[0] % 200 == 0:
            progress("更新/校验K线 %d/%d" % (done[0], total),
                     0.10 + 0.55 * done[0] / max(total, 1))
        if len(bars) < sc["min_history_days"]:
            return None
        seg = bars[-340:]
        closes = [b["c"] for b in seg]
        n = len(closes)
        if n < 61:
            return None
        ma60 = sum(closes[-60:]) / 60.0
        ret20 = closes[-1] / closes[-21] - 1.0 if n >= 21 else 0.0
        # 紧凑数组缓存（array('d') 内存友好），供打分/理由阶段直接复用，免二次读库
        return {
            "above": closes[-1] > ma60, "ret20": ret20, "last": seg[-1]["date"],
            "o": array("d", [b["o"] for b in seg]),
            "h": array("d", [b["h"] for b in seg]),
            "l": array("d", [b["l"] for b in seg]),
            "c": array("d", closes),
            "v": array("d", [b["v"] or 0.0 for b in seg]),
        }

    metrics = dataapi.parallel(cands, preload, workers=workers)
    ok_codes = []
    recs = {}
    for i, m in enumerate(cands):
        mt = metrics[i]
        if mt is None:
            continue
        coverage[m["board"]]["with_kline"] += 1
        ok_codes.append(m["code"])
        recs[m["code"]] = mt
    log("K线就绪 %d/%d 只" % (len(ok_codes), total), 0.66)
    if not ok_codes:
        return {"picks": [], "coverage": coverage,
                "meta": {"error": "无足够K线数据（数据源异常？）"}}

    # ---- 4. 行业景气 + 大盘上下文 ----
    log("构建行业景气与大盘上下文…", 0.70)
    sector = {}
    for i, m in enumerate(cands):
        mt = metrics[i]
        if mt is None:
            continue
        ind = m["industry"]
        s = sector.setdefault(ind, {"members": 0, "above": 0, "rets": []})
        s["members"] += 1
        if mt["above"]:
            s["above"] += 1
        s["rets"].append(mt["ret20"])
    sector_stats = {ind: {"members": s["members"],
                          "above_frac": s["above"] / max(1, s["members"]),
                          "ret20": sum(s["rets"]) / max(1, len(s["rets"]))}
                    for ind, s in sector.items()}

    bench_closes = [b["c"] for b in bench]
    ctx = engine.EngineCtx()
    ctx.bench_ok = len(bench) >= 22
    ctx.bench_closes = bench_closes
    ctx.bench_pos = len(bench) - 1
    ctx.sector = sector_stats
    ctx.funds = provider.fundamentals()
    ctx.date = date

    # ---- 5. 全维度打分（第一遍：只算分） ----
    log("全维度打分（%d 只）…" % len(ok_codes), 0.75)
    threshold = sc["min_score"]
    scored = []
    n_ok = len(ok_codes)
    for n_i, code in enumerate(ok_codes):
        m = meta_by_code.get(code)
        rec = recs.get(code)
        if rec is None:
            continue
        t = len(rec["c"]) - 1
        if t < 120 or rec["last"] != date:
            continue
        dates = [None] * t + [rec["last"]]
        arr = engine.build_arrays_lists(rec["o"], rec["h"], rec["l"], rec["c"],
                                        rec["v"], dates, m.get("shares_float"))
        ev = engine.evaluate(code, arr, m, t, ctx, params, reasons=False)
        scored.append((ev["score"], code, m, ev))
        if ev["score"] >= threshold:
            coverage[m["board"]]["passed"] += 1
        if progress and (n_i + 1) % 500 == 0:
            progress("打分 %d/%d" % (n_i + 1, n_ok), 0.75 + 0.15 * (n_i + 1) / max(1, n_ok))
    scored.sort(key=lambda x: x[0], reverse=True)

    # ---- 6. 选择：板块保底 + 上限 + 门槛放宽 ----
    picks = _select(scored, params, threshold, sc["top_n"])
    relaxed = False
    floor = max(35.0, sc["min_score"] - 25.0)
    while len(picks) < 3 and sc.get("auto_relax", True) and threshold > floor:
        threshold = round(threshold - 5.0, 1)
        relaxed = True
        picks = _select(scored, params, threshold, sc["top_n"])

    # ---- 7. 为入选票生成完整理由（第二遍，复用预热缓存） ----
    out_picks = []
    for score, code, m, ev in picks:
        rec = recs.get(code)
        if rec is None:
            continue
        t = len(rec["c"]) - 1
        dates = [None] * t + [rec["last"]]
        arr = engine.build_arrays_lists(rec["o"], rec["h"], rec["l"], rec["c"],
                                        rec["v"], dates, m.get("shares_float"))
        full = engine.evaluate(code, arr, m, t, ctx, params, reasons=True)
        out_picks.append(pick_payload(code, m, arr, t, ctx, params, full))
        coverage[m["board"]]["picked"] += 1

    meta_out = {
        "date": date, "provider": provider.mode, "provider_label": provider.label,
        "params_version": params.get("version"), "threshold": threshold,
        "relaxed": relaxed,
        "relaxed_note": ("未达原始门槛(%.0f分)，已自动放宽至 %.0f 分" % (
            sc["min_score"], threshold)) if relaxed else None,
        "counts": {
            "universe": len(meta_list), "candidate": len(cands),
            "with_kline": len(ok_codes), "scored": len(scored),
            "picked": len(out_picks),
            "suspended": sum(c["suspended"] for c in coverage.values()),
        },
        "coverage": coverage,
        "boards": list(config.BOARDS),
        "universe_policy": "全市场全量扫描（沪主板/深主板/创业板/科创板/北证），不抽样、不遗漏",
    }
    log("选股完成：全市场 %d 只，候选 %d，K线 %d，入选 %d 只（%s）" % (
        len(meta_list), len(cands), len(ok_codes), len(out_picks), date), 1.0)
    return {"picks": out_picks, "meta": meta_out, "coverage": coverage}
