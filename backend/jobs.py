# -*- coding: utf-8 -*-
"""业务编排：把 provider/screen/backtest/tune 串成可调度的任务。

包含：数据刷新、每日选股(含落盘)、虚拟持仓跟踪与平仓记录、回测报告保存、自调优。
"""
import json
import os
import threading

from . import config, provider as provider_mod, store
from .strategy import backtest as bt_mod, params as pmod, screen as screen_mod, tune as tune_mod

_LOCK = threading.Lock()


def get_provider():
    return provider_mod.create_provider(config.load_settings().get("data_source", "auto"))


# ---------------------------------------------------------------------------
def refresh_fundamentals(progress=None):
    """刷新基本面快照（最新报告期 ROE/净利增速/行业归属）。"""
    p = get_provider()
    if progress:
        progress("刷新业绩报表（ROE/净利增速）…", 0.2)
    res = p.refresh_fundamentals()
    if progress:
        progress("基本面就绪：%d 条（报告期 %s）" % (res["count"], res["report_date"]), 1.0)
    return res


# ---------------------------------------------------------------------------
def run_screen(progress=None, force=False):
    """每日选股并把结果写入 picks.json，同时更新虚拟持仓簿。"""
    if progress:
        progress("开始每日选股…", 0.01)
    p = get_provider()
    params = config.load_params()

    last = store.meta_get("last_screen_date", "")
    if last == params.get("version") + "@" + (p.today_str() or "") and not force:
        return {"skipped": True, "note": "今日已按当前参数完成选股"}

    # 实时模式：先刷新最新报告期业绩（含行业归属与 ROE/增速），一天一次
    if p.mode == "live":
        try:
            today = config.now_str()[:10]
            if store.meta_get("fund_day", "") != today:
                if progress:
                    progress("刷新业绩报表与行业归属…", 0.03)
                p.refresh_fundamentals()
                store.meta_set("fund_day", today)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress("业绩刷新失败（%s），继续使用缓存" % str(e)[:60], 0.04)

    result = screen_mod.screen(p, params, progress=progress)
    result["meta"]["params_version"] = params.get("version")
    result["meta"]["screen_at"] = config.now_str()
    result["meta"]["param_summary"] = pmod.param_summary(params)
    store.json_write(config.PICKS_PATH, result)
    store.meta_set("last_screen_date", params.get("version") + "@" + (result["meta"].get("date") or ""))
    store.meta_set("last_screen_at", config.now_str())

    # 更新虚拟持仓簿（把新入选票加入跟踪）
    update_tracking(p, progress=None)
    if progress:
        progress("选股完成：入选 %d 只（%s）" % (
            len(result["picks"]), result["meta"].get("date", "")), 1.0)
    return result


# ---------------------------------------------------------------------------
def run_backtest(progress=None):
    """执行回测并保存报告到 data/reports/backtest_*.json。"""
    p = get_provider()
    params = config.load_params()
    if progress:
        progress("回测开始：宇宙 %s" % p.label, 0.02)
    bt = bt_mod.Backtester(p, progress=progress)
    bt.prepare()
    report = bt.run(params, progress=progress)
    ver = str(params.get("version", "v1")).replace(".", "_").replace("v", "v")
    fname = "backtest_%s_%s.json" % (
        config.now_str().replace(":", "").replace(" ", "_").replace("-", ""), ver)
    path = os.path.join(config.REPORTS_DIR, fname)
    store.json_write(path, report)
    store.json_write(os.path.join(config.REPORTS_DIR, "latest_backtest.json"), report)
    if progress:
        s = report["stats"]
        progress("回测报告已保存：%d 笔，胜率 %.1f%%" % (
            s["n_trades"], (s["win_rate"] or 0) * 100), 1.0)
    return {"report_path": path, "report": report}


def run_tune(progress=None, combos=None):
    """执行回测驱动的参数自调优（内部会做多次回测，较耗时）。"""
    p = get_provider()
    params = config.load_params()
    if progress:
        progress("启动自调优（组合数 %d）…" % (combos or 18), 0.01)
    result = tune_mod.run_tune(p, params, combos=combos or 18, progress=progress)
    result["at"] = config.now_str()
    # 调优采纳新参数后，立即用新参数重跑一次最新选股
    if result.get("adopted") and progress:
        progress("参数已更新，用新参数重新选股…", 0.93)
    if result.get("adopted"):
        try:
            run_screen(progress=progress, force=True)
        except Exception as e:  # noqa: BLE001
            result["rescreen_error"] = str(e)
    if progress:
        progress("自调优完成", 1.0)
    return result


# ---------------------------------------------------------------------------
# 虚拟持仓簿（纸面交易）：跟踪入选票 → 统计“成功率/盈亏比”的真实累计
# ---------------------------------------------------------------------------
def _ledger_path():
    return os.path.join(config.DATA_DIR, "paper_ledger.json")


def update_tracking(provider=None, progress=None):
    """用最新行情更新虚拟持仓：破止损/破MA20/超时 → 平仓并累计到交易记录。"""
    p = provider or get_provider()
    picks = store.json_read(config.PICKS_PATH, {}) or {}
    picks_list = picks.get("picks") or []
    ledger = store.json_read(_ledger_path(), {"positions": [], "closed": []}) or {}

    by_code = {x["code"]: x for x in picks_list}
    now = config.now_str()
    changed = False

    # 1) 新入选 → 开仓
    seen = {pos["code"] for pos in ledger["positions"]}
    for pk in picks_list:
        if pk["code"] in seen:
            continue
        ledger["positions"].append({
            "code": pk["code"], "name": pk["name"], "board": pk["board"],
            "industry": pk["industry"],
            "entry_date": pk.get("entry_date") or picks.get("meta", {}).get("date"),
            "entry_price": pk.get("entry_price") or pk["price"],
            "stop_price": pk["stop_price"], "stop_why": pk["stop_why"],
            "score": pk["score"], "opened_at": now,
            "status": "open", "note": "信号日入选自动建立虚拟持仓",
        })
        changed = True

    # 2) 逐日盯盘（用最新 bars）
    if changed:
        store.json_write(_ledger_path(), ledger)

    for pos in ledger["positions"]:
        if pos["status"] != "open":
            continue
        try:
            bars = p.bars(pos["code"])
        except Exception:  # noqa: BLE001
            continue
        if not bars:
            continue
        last = bars[-1]
        cur = last["c"]
        # 破止损（盘中最低价 ≤ 止损价）
        if pos["stop_price"] and last["l"] <= pos["stop_price"]:
            fill = min(last["o"], pos["stop_price"])
            pos.update({"status": "closed", "exit_date": last["date"],
                        "exit_price": fill, "exit_reason": "止损破位",
                        "ret": fill / pos["entry_price"] - 1.0, "closed_at": now})
            ledger["closed"].append(dict(pos))
            changed = True
            continue
        # 破位坚决跑：收盘跌破 MA20
        closes = [b["c"] for b in bars[-25:]]
        ma20 = sum(closes) / len(closes) if closes else None
        if ma20 and cur < ma20 and pos.get("entry_date", "") < last["date"]:
            pos.update({"status": "closed", "exit_date": last["date"],
                        "exit_price": cur, "exit_reason": "跌破MA20趋势破坏",
                        "ret": cur / pos["entry_price"] - 1.0, "closed_at": now})
            ledger["closed"].append(dict(pos))
            changed = True
            continue
        # 持有超时（约 N 个交易日）仍未创新高则退出
        n_days = 0
        for b in reversed(bars):
            if b["date"] >= (pos.get("entry_date") or "1900-01-01"):
                n_days += 1
            else:
                break
        timeout = config.load_params()["risk"]["timeout_days"]
        if n_days > timeout and pos.get("exit_reason") is None:
            pos.update({"status": "closed", "exit_date": last["date"],
                        "exit_price": cur, "exit_reason": "持有超时未再上攻",
                        "ret": cur / pos["entry_price"] - 1.0, "closed_at": now})
            ledger["closed"].append(dict(pos))
            changed = True
            continue
        pos["status"] = "open"
        pos["current_price"] = cur
        pos["pct"] = cur / pos["entry_price"] - 1.0
        pos["last_date"] = last["date"]
        pos["note"] = "最新收盘 %.2f，止损线 %.2f" % (cur, pos["stop_price"])

    if changed:
        ledger["positions"] = [x for x in ledger["positions"] if x["status"] != "closed"]
        store.json_write(_ledger_path(), ledger)
    return summarize_paper(ledger)


def summarize_paper(ledger=None):
    ledger = ledger or store.json_read(_ledger_path(), {"positions": [], "closed": []}) or {}
    closed = ledger.get("closed") or []
    n = len(closed)
    wins = [c for c in closed if (c.get("ret") or 0) > 0]
    losses = [c for c in closed if (c.get("ret") or 0) <= 0]
    stats = {
        "n_trades": n, "n_open": len(ledger.get("positions") or []),
        "n_win": len(wins), "n_loss": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
    }
    if wins:
        stats["avg_win"] = round(sum(c["ret"] for c in wins) / len(wins), 4)
    if losses:
        stats["avg_loss"] = round(sum(c["ret"] for c in losses) / len(losses), 4)
    if wins and losses:
        stats["payoff"] = round(stats["avg_win"] / abs(stats["avg_loss"]), 3)
    return {"positions": ledger.get("positions") or [], "closed": closed, "stats": stats}


# ---------------------------------------------------------------------------
# 最近回测报告
# ---------------------------------------------------------------------------
def latest_backtest():
    return store.json_read(os.path.join(config.REPORTS_DIR, "latest_backtest.json"))


def list_backtests(limit=20):
    out = []
    d = config.REPORTS_DIR
    if not os.path.isdir(d):
        return out
    files = sorted(os.listdir(d), reverse=True)
    for f in files:
        if f.startswith("backtest_") and f.endswith(".json") and f != "latest_backtest.json":
            r = store.json_read(os.path.join(d, f))
            if r:
                s = r.get("stats", {})
                out.append({"file": f, "period": r.get("period"),
                            "version": r.get("params_version"),
                            "n_trades": s.get("n_trades"), "win_rate": s.get("win_rate"),
                            "payoff": s.get("payoff"),
                            "param_summary": r.get("param_summary")})
            if len(out) >= limit:
                break
    return out
