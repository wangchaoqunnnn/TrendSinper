# -*- coding: utf-8 -*-
"""回测引擎：在历史K线上按固定节奏重放“选股→持有→离场”流程。

- 每 step_days 个交易日做一次全市场打分选股（与实盘 screen 同一套规则与参数）；
- 买入价=信号日收盘；离场：盘中跌破止损价(按开盘跳空价/止损价成交) / 收盘跌破MA20 /
  持有超时 / 期末强制平仓；
- 输出：成功率、盈亏比(avgWin/avgLoss)、利润因子、每笔期望、最大回撤、资金曲线、
  按板块/退出原因/是否创新高分组统计。

方法说明（报告中一并展示）：
  · 基本面采用“当前最新可得”报告期数据 —— 跨期对比存在轻微前视偏差，技术面规则不受影响；
  · 行业景气统计基于按板块分层抽样的回测宇宙（≤240 只，覆盖全部板块与主要行业）。
"""
import time

from .. import config
from . import engine, params as pmod, screen as screen_mod


class Backtester:
    def __init__(self, provider, progress=None):
        self.provider = provider
        self.progress = progress
        self.universe = []          # [(code, meta, arr)]
        self.meta_by_code = {}
        self.arr_by_code = {}
        self.date_idx = {}          # code -> {date: idx}
        self.bench_closes = []
        self.bench_dates = []
        self.bench_pos = {}         # date -> pos in bench
        self.funds = {}

    def _log(self, msg, frac=None):
        if self.progress:
            self.progress(msg, frac)

    # ------------------------------------------------------------------
    def prepare(self, max_universe=240, per_industry=5):
        """构建回测宇宙并预计算技术数组（与调参候选无关 → 可被多轮复用以提速）。"""
        self._log("构建回测宇宙（按行业抽样）…", 0.02)
        metas = self.provider.universe_cached()
        if not metas:
            metas = self.provider.snapshot()
        keep = [m for m in metas
                if not m.get("is_st") and (m.get("price") or 0) >= 3.0
                and m.get("float_mv") is not None
                and 15e8 <= m["float_mv"] <= 2500e8]
        by_ind = {}
        for m in keep:
            by_ind.setdefault(m["industry"], []).append(m)
        universe = []
        for ind in sorted(by_ind.keys()):
            universe.extend(by_ind[ind][:per_industry])

        # 按板块分层均衡抽样：保证每个板块都有代表，再全局补足到 max_universe
        import random as _random
        rng = _random.Random(20260904)
        by_board = {}
        for m in universe:
            by_board.setdefault(m["board"], []).append(m)
        chosen = []
        avail_total = len(universe)
        for b in sorted(by_board.keys()):
            rng.shuffle(by_board[b])
        if len(universe) > max_universe:
            for b in sorted(by_board.keys()):
                n_b = max(2, round(max_universe * len(by_board[b]) / avail_total))
                chosen.extend(by_board[b][:n_b])
            rest = [m for m in universe if m not in chosen]
            rng.shuffle(rest)
            chosen.extend(rest[:max_universe - len(chosen)])
            universe = chosen
        else:
            universe = [m for lst in by_board.values() for m in lst]

        from .. import dataapi
        done = [0]

        def load(m):
            bars = self.provider.bars(m["code"], deep=True)
            done[0] += 1
            if done[0] % 40 == 0:
                self._log("回测K线 %d/%d" % (done[0], len(universe)),
                          0.05 + 0.4 * done[0] / max(1, len(universe)))
            if len(bars) < 320:
                return None
            return engine.build_arrays(bars, shares_float=m.get("shares_float"))

        results = dataapi.parallel(universe, load, workers=8)
        for i, m in enumerate(universe):
            a = results[i]
            if a is None:
                continue
            self.universe.append((m["code"], m, a))
            self.date_idx[m["code"]] = {d: i for i, d in enumerate(a["dates"])}
        self.meta_by_code = {c: m for c, m, _ in self.universe}
        self.arr_by_code = {c: a for c, _, a in self.universe}
        if not self.universe:
            raise RuntimeError("回测宇宙为空：请先执行一次数据刷新")
        self._log("宇宙就绪：%d 只" % len(self.universe), 0.5)

        bench = self.provider.index_bars()
        if not bench:
            raise RuntimeError("无法获取基准指数K线")
        self.bench_closes = [b["c"] for b in bench]
        self.bench_dates = [b["date"] for b in bench]
        self.bench_pos = {d: i for i, d in enumerate(self.bench_dates)}
        self.funds = self.provider.fundamentals()
        self._log("指数与基本面就绪", 0.55)

    # ------------------------------------------------------------------
    def _sector_stats(self, cal_idx):
        """在基准日历第 cal_idx 天统计各行业站上MA60比例与20日涨幅。"""
        date = self.bench_dates[cal_idx]
        stats = {}
        for code, m, a in self.universe:
            i = self.date_idx[code].get(date)
            if i is None or i < 20 or a["ma60"][i] is None:
                continue
            s = stats.setdefault(m["industry"], {"members": 0, "above": 0, "rets": []})
            s["members"] += 1
            if a["c"][i] > a["ma60"][i]:
                s["above"] += 1
            s["rets"].append(a["c"][i] / a["c"][i - 20] - 1.0)
        out = {}
        for ind, s in stats.items():
            out[ind] = {"members": s["members"],
                        "above_frac": s["above"] / max(1, s["members"]),
                        "ret20": sum(s["rets"]) / max(1, len(s["rets"]))}
        return out

    # ------------------------------------------------------------------
    def run(self, params=None, progress=None, sample=None):
        params = params or config.load_params()
        bt = params["backtest"]
        if not self.universe:
            self.prepare()
        self._log("回测开始…", 0.58)
        t0 = time.time()
        top_n = bt.get("top_n", 8)
        step = bt.get("step_days", 4)
        cost = bt.get("cost", 0.0015)
        timeout_days = params["risk"]["timeout_days"]
        self._timeout_days = timeout_days
        min_score = params["screen"]["min_score"]
        cap = screen_mod._board_cap(params)
        hist_days = bt.get("history_days", 360)

        total = len(self.bench_dates)
        need = hist_days + 310
        start_cal = max(0, total - need)
        end_cal = total - 1
        if sample:
            s0 = self.bench_pos.get(sample[0])
            e0 = self.bench_pos.get(sample[1])
            if s0 is not None and e0 is not None:
                start_cal, end_cal = s0, e0
        cal_dates = self.bench_dates

        step_idxs = list(range(start_cal, end_cal + 1, step))
        trades = []
        open_pos = {}

        def advance(tr, upto_cal):
            """逐日检查持仓至 upto_cal，返回最早触发离场信息或 None。"""
            code = tr["code"]
            a = self.arr_by_code[code]
            for cal_idx in range(tr["entry_cal"] + 1, upto_cal + 1):
                date = cal_dates[cal_idx]
                i = self.date_idx[code].get(date)
                if i is None:
                    continue
                stop = tr["stop_price"]
                if stop and a["l"][i] <= stop:
                    # 跳空低开时按开盘价成交，否则按止损价成交
                    fill = min(a["o"][i], stop)
                    return {"exit_date": date, "exit_price": fill,
                            "exit_reason": "止损破位"}
                if a["ma20"][i] is not None and a["c"][i] < a["ma20"][i]:
                    return {"exit_date": date, "exit_price": a["c"][i],
                            "exit_reason": "跌破MA20趋势破坏"}
                if cal_idx - tr["entry_cal"] >= self._timeout_days:
                    return {"exit_date": date, "exit_price": a["c"][i],
                            "exit_reason": "持有超时未再上攻"}
            return None

        # ---- 主循环：每个信号日先处理既有持仓，再做当日选股 ----
        exited_today = set()
        n_steps = len(step_idxs)
        for k, cal_idx in enumerate(step_idxs):
            date = cal_dates[cal_idx]
            # 1) 推进全部持仓到本信号日（盘中/收盘离场）
            for code, tr in list(open_pos.items()):
                ex = advance(tr, cal_idx)
                if ex:
                    tr.update(ex)
                    tr["closed"] = True
                    trades.append(tr)
                    del open_pos[code]
                    exited_today.add(code)

            # 2) 当日选股（收盘价买入）
            sector = self._sector_stats(cal_idx)
            ctx = engine.EngineCtx()
            ctx.date = date
            ctx.bench_ok = True
            ctx.bench_closes = self.bench_closes
            ctx.bench_pos = cal_idx
            ctx.sector = sector
            ctx.funds = self.funds

            candidates = []
            for code, m, a in self.universe:
                if code in open_pos or code in exited_today:
                    continue
                i = self.date_idx[code].get(date)
                if i is None or i < 260:      # 保证 MA120/60/250窗口数据完整
                    continue
                price = a["c"][i]
                if not (params["screen"]["min_price"] <= price <= params["screen"]["max_price"]):
                    continue
                ev = engine.evaluate(code, a, m, i, ctx, params, reasons=False)
                if ev["score"] >= min_score:
                    candidates.append((ev["score"], code, ev, a, m, i))
            candidates.sort(key=lambda x: x[0], reverse=True)

            board_cnt = {}
            for score, code, ev, a, m, i in candidates:
                b = m["board"]
                if board_cnt.get(b, 0) >= cap:
                    continue
                board_cnt[b] = board_cnt.get(b, 0) + 1
                entry = a["c"][i]
                stop_price, stop_why = screen_mod.compute_stop(entry, a, i, params)
                open_pos[code] = {
                    "code": code, "name": m["name"], "board": m["board"],
                    "industry": m["industry"], "entry_date": date,
                    "entry_cal": cal_idx, "entry_price": entry,
                    "stop_price": stop_price, "stop_why": stop_why,
                    "score": ev["score"], "new_high": ev["new_high"],
                    "closed": False,
                }
                if len(open_pos) >= max(top_n * 2, 12):
                    break
            exited_today.clear()
            if (k + 1) % 15 == 0 or k == n_steps - 1:
                self._log("回测推进 %d/%d 信号日" % (k + 1, n_steps),
                          0.58 + 0.34 * (k + 1) / max(1, n_steps))

        # ---- 期末强制平仓 ----
        for code, tr in list(open_pos.items()):
            if tr.get("closed"):
                continue
            i = self.date_idx[code].get(cal_dates[end_cal])
            a = self.arr_by_code[code]
            if i is None:
                i = len(a["c"]) - 1
            tr.update({"exit_date": cal_dates[end_cal], "exit_price": a["c"][i],
                       "exit_reason": "期末未平仓", "closed": True})
            trades.append(tr)

        # ---- 结算 ----
        for tr in trades:
            tr["ret"] = (tr["exit_price"] * (1 - cost)) / (tr["entry_price"] * (1 + cost)) - 1.0
            e_cal = self.bench_pos.get(tr["exit_date"])
            en_cal = self.bench_pos.get(tr["entry_date"])
            tr["hold_days"] = (e_cal - en_cal) if (e_cal is not None and en_cal is not None) else None

        report = self._report(trades, params, cal_dates[start_cal], cal_dates[end_cal],
                              len(step_idxs), time.time() - t0)
        s = report["stats"]
        self._log("回测完成：%d 笔，胜率 %.1f%%" % (s["n_trades"], (s["win_rate"] or 0) * 100), 0.97)
        return report

    # ------------------------------------------------------------------
    def _report(self, trades, params, start_date, end_date, screened_days, elapsed):
        n = len(trades)
        wins = [t["ret"] for t in trades if t["ret"] > 0]
        losses = [t["ret"] for t in trades if t["ret"] <= 0]
        win_rate = len(wins) / n if n else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        payoff = (avg_win / abs(avg_loss)) if losses and avg_loss < 0 else None
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_win / gross_loss if gross_loss > 0 else None
        expectancy = (sum(t["ret"] for t in trades) / n) if n else 0.0

        value, peak, mdd = 1.0, 1.0, 0.0
        curve = []
        w_per_trade = 1.0 / 12.0   # 等权名义仓位：每笔约 1/12 资金（示意净值）
        for t in sorted(trades, key=lambda x: (x["exit_date"], x["code"])):
            value *= (1 + w_per_trade * t["ret"])
            curve.append({"date": t["exit_date"], "value": round(value, 4)})
            peak = max(peak, value)
            mdd = max(mdd, 1 - value / peak)

        def group(keyfn):
            g = {}
            for t in trades:
                key = keyfn(t)
                gg = g.setdefault(key, {"n": 0, "win": 0, "rets": []})
                gg["n"] += 1
                gg["win"] += 1 if t["ret"] > 0 else 0
                gg["rets"].append(t["ret"])
            out = {}
            for k, gg in g.items():
                w = [r for r in gg["rets"] if r > 0]
                l = [r for r in gg["rets"] if r <= 0]
                avg_w = (sum(w) / len(w)) if w else 0.0
                avg_l = (sum(l) / len(l)) if l else 0.0
                pf = (avg_w / abs(avg_l)) if (l and avg_l < 0 and w) else None
                out[k] = {
                    "n": gg["n"], "win_rate": round(gg["win"] / gg["n"], 4),
                    "avg_win": round(avg_w, 4),
                    "avg_loss": round(avg_l, 4),
                    "payoff": round(pf, 3) if pf is not None else None,
                }
            return out

        by_new_high = {"是": 0, "否": 0}
        for t in trades:
            by_new_high["是" if t.get("new_high") else "否"] += 1

        return {
            "params_version": params.get("version"),
            "param_summary": pmod.param_summary(params),
            "period": [start_date, end_date],
            "screened_days": screened_days,
            "universe": len(self.universe),
            "elapsed_s": round(elapsed, 1),
            "caveat": ("技术面规则完全基于截至信号日的历史K线；基本面采用当前最新报告期数据，"
                       "跨期回测存在轻微前视偏差；实时模式行业景气基于抽样宇宙；"
                       "计入双边成本 %.2f%%。" % (params["backtest"]["cost"] * 200)),
            "stats": {
                "n_trades": n, "n_win": len(wins), "n_loss": len(losses),
                "win_rate": round(win_rate, 4),
                "avg_win": round(avg_win, 4), "avg_loss": round(avg_loss, 4),
                "payoff": round(payoff, 3) if payoff is not None else None,
                "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
                "expectancy": round(expectancy, 4),
                "total_return": round(value - 1.0, 4),
                "max_drawdown": round(mdd, 4),
                "avg_hold_days": round(sum(t.get("hold_days") or 0 for t in trades) / n, 1) if n else 0,
            },
            "by_board": group(lambda t: t["board"]),
            "by_reason": group(lambda t: t["exit_reason"] or "未知"),
            "by_new_high": by_new_high,
            "equity": curve,
            "trades": sorted(trades, key=lambda x: (x["entry_date"], x["code"])),
        }


def objective(stats, params=None):
    """调优目标：胜率×0.35 + 盈亏比(封顶3)×0.30 + 每笔期望(封顶0.10)×0.20 + 利润因子(封顶2)×0.15"""
    ow = (params or config.load_params())["backtest"]["objective_weights"]
    if not stats or stats["n_trades"] == 0:
        return -1.0
    wr = stats["win_rate"] or 0.0
    pf = min(stats["payoff"] or 0.0, 3.0) / 3.0
    ex = min(max(stats["expectancy"] or 0.0, 0.0), 0.10) / 0.10
    prf = min(stats["profit_factor"] or 0.0, 2.0) / 2.0
    return (wr * ow.get("win_rate", 0.35) + pf * ow.get("payoff", 0.30)
            + ex * ow.get("expectancy", 0.20) + prf * ow.get("profit_factor", 0.15))
