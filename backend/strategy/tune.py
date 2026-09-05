# -*- coding: utf-8 -*-
"""策略自调优：以回测结果驱动，自动搜索更优参数并落盘。

流程：训练窗(历史前60%)随机搜索 → 验证窗(后40%)确认 → 显著更优才采纳，
避免在单一区间上过拟合；每次调优写入 tuning_log（含决策理由），供页面回看。
"""
import json
import random
import time

from .. import config, store
from . import backtest as bt_mod
from . import params as pmod


def _cmp_stats(a, b):
    """把两个统计 dict 的中文差异写成说明文本。"""
    def pct(v):
        return "%.1f%%" % (v * 100) if v is not None else "—"
    if not a or not b:
        return "样本不足"
    return (
        "胜率 %s→%s；盈亏比 %s→%s；每笔期望 %+.2f%%→%+.2f%%；利润因子 %s→%s；"
        "最大回撤 %s→%s" % (
            pct(a.get("win_rate")), pct(b.get("win_rate")),
            _f(a.get("payoff")), _f(b.get("payoff")),
            (a.get("expectancy") or 0) * 100, (b.get("expectancy") or 0) * 100,
            _f(a.get("profit_factor")), _f(b.get("profit_factor")),
            pct(a.get("max_drawdown")), pct(b.get("max_drawdown"))))


def _f(v, nd=2):
    return "%.2f" % v if v is not None else "—"


def _changed_summary(before, after):
    """比较两版参数，返回中文变更说明。"""
    lines = []
    b_r, a_r = before["rules"], after["rules"]
    if a_r["month_ret"]["min"] != b_r["month_ret"]["min"] or \
            a_r["month_ret"]["max"] != b_r["month_ret"]["max"]:
        lines.append("月涨幅区间 %.0f%%~%.0f%% → %.0f%%~%.0f%%" % (
            b_r["month_ret"]["min"] * 100, b_r["month_ret"]["max"] * 100,
            a_r["month_ret"]["min"] * 100, a_r["month_ret"]["max"] * 100))
    if a_r["turnover"]["min"] != b_r["turnover"]["min"] or \
            a_r["turnover"]["max"] != b_r["turnover"]["max"]:
        lines.append("换手区间 %.1f%%~%.1f%% → %.1f%%~%.1f%%" % (
            b_r["turnover"]["min"] * 100, b_r["turnover"]["max"] * 100,
            a_r["turnover"]["min"] * 100, a_r["turnover"]["max"] * 100))
    if a_r["ma_bull"]["mid_rising_days"] != b_r["ma_bull"]["mid_rising_days"]:
        lines.append("MA60上行确认天数 %d→%d" % (
            b_r["ma_bull"]["mid_rising_days"], a_r["ma_bull"]["mid_rising_days"]))
    if a_r["new_high"]["windows"] != b_r["new_high"]["windows"]:
        lines.append("创新高窗口 [%s]→[%s]" % (
            ",".join(map(str, b_r["new_high"]["windows"])),
            ",".join(map(str, a_r["new_high"]["windows"]))))
    if after["risk"]["stop_pct"] != before["risk"]["stop_pct"]:
        lines.append("止损比例 %.1f%%→%.1f%%" % (
            before["risk"]["stop_pct"] * 100, after["risk"]["stop_pct"] * 100))
    if after["risk"]["timeout_days"] != before["risk"]["timeout_days"]:
        lines.append("持有超时天数 %d→%d" % (
            before["risk"]["timeout_days"], after["risk"]["timeout_days"]))
    if after["screen"]["min_score"] != before["screen"]["min_score"]:
        lines.append("入选门槛 %.0f→%.0f 分" % (
            before["screen"]["min_score"], after["screen"]["min_score"]))
    w_changes = []
    for k in before["weights"]:
        if abs(after["weights"].get(k, 0) - before["weights"].get(k, 0)) > 0.015:
            w_changes.append("%s:%.2f→%.2f" % (k, before["weights"][k],
                                               after["weights"].get(k, 0)))
    if w_changes:
        lines.append("维度权重调整 " + "，".join(w_changes[:6]))
    return "；".join(lines) if lines else "（仅数值微调）"


def run_tune(provider=None, params=None, combos=18, progress=None):
    """执行一次“回测驱动自调优”。provider 可复用已建好的 Backtester。"""
    def log(m, f=None):
        if progress:
            progress(m, f)

    params = params or config.load_params()
    log("准备调优（构建回测宇宙，首次较慢）…", 0.03)
    bt = bt_mod.Backtester(provider)
    bt.prepare()
    log("宇宙构建完成，开始训练窗搜索…", 0.08)

    # 划分训练/验证窗（按基准日历）
    n_b = len(bt.bench_dates)
    full_start = max(0, n_b - (params["backtest"]["history_days"] + 310))
    mid = full_start + int((n_b - 1 - full_start) * 0.6)
    train = (bt.bench_dates[full_start], bt.bench_dates[mid])
    test = (bt.bench_dates[mid + 1] if mid + 1 < n_b else bt.bench_dates[mid],
            bt.bench_dates[-1])

    log("训练窗回测（基准参数）…", 0.15)
    base_train = bt.run(params, sample=train)
    base_obj = bt_mod.objective(base_train["stats"], params)
    log("基准训练窗 目标 %.3f（%d 笔）" % (base_obj, base_train["stats"]["n_trades"]), 0.2)

    best = params
    best_obj = base_obj
    best_report = base_train
    best_keys = []
    min_trades = params["backtest"]["min_trades"]
    rng = random.Random()

    for i in range(combos):
        cand, keys = pmod.sample_candidate(params, rng)
        log("候选 %d/%d 回测中…" % (i + 1, combos), 0.2 + 0.55 * (i + 1) / combos)
        rep = bt.run(cand, sample=train)
        st = rep["stats"]
        if st["n_trades"] < min_trades:
            continue
        o = bt_mod.objective(st, cand)
        if o > best_obj + 1e-6:
            best, best_obj, best_report, best_keys = cand, o, rep, keys
        time.sleep(0)

    adopted = False
    note = ""
    diff_text = ""
    if best_obj > base_obj + 0.004 and best_report["stats"]["n_trades"] >= min_trades:
        # 验证窗确认
        log("候选在验证窗确认…", 0.8)
        base_test = bt.run(params, sample=test)
        best_test = bt.run(best, sample=test)
        b_t, p_t = base_test["stats"], best_test["stats"]
        if p_t["n_trades"] >= min_trades and bt_mod.objective(p_t, best) > \
                bt_mod.objective(b_t, params) - 0.0:
            adopted = True
            diff_text = _changed_summary(params, best)
            note = (
                "自调优采纳：训练窗%s；验证窗%s。变更：%s。"
                "依据《趋势策略.md》：形态看均线、买卖看支撑 —— 参数调整不改变策略骨架，"
                "仅微调入场节奏与风控边界。" % (
                    _cmp_stats(base_train["stats"], best_report["stats"]),
                    _cmp_stats(b_t, p_t), diff_text))
        else:
            note = ("候选训练窗更优但验证窗未确认（防过拟合），本次维持当前参数。"
                    "训练窗%s；验证窗%s" % (
                        _cmp_stats(base_train["stats"], best_report["stats"]),
                        _cmp_stats(b_t, p_t)))
    else:
        note = ("本次搜索未找到显著更优参数：最优候选目标 %.3f 相对基准 %.3f 提升不足，"
                "维持 v%s 不变（避免在噪声上过拟合）。" % (best_obj, base_obj, params.get("version")))

    # 写日志（无论是否采纳都记录本轮结论）
    if adopted:
        new_ver = _bump_version(params.get("version", "v1.0"))
        best["version"] = new_ver
        best["tuned_at"] = config.now_str()
        config.save_params(best)
        store.add_tuning_log("回测驱动随机搜索", best, best_report["stats"], note)
        result = {"adopted": True, "version": new_ver, "note": note,
                  "before_stats": base_train["stats"],
                  "after_stats": best_report["stats"],
                  "changed": diff_text}
    else:
        store.add_tuning_log("回测驱动随机搜索", params, base_train["stats"], note)
        result = {"adopted": False, "version": params.get("version"), "note": note,
                  "before_stats": base_train["stats"],
                  "after_stats": base_train["stats"],
                  "changed": ""}
    log("调优完成：" + ("已采纳并更新策略参数" if adopted else "维持原参数"), 1.0)
    return result


def _bump_version(v):
    """v1.0 -> v1.1"""
    try:
        s = v.lstrip("v").split(".")
        major, minor = int(s[0]), int(s[1]) if len(s) > 1 else 0
        return "v%d.%d" % (major, minor + 1)
    except Exception:
        return v + ".1"
