# -*- coding: utf-8 -*-
"""命令行入口：python -m backend.main <serve|screen|backtest|tune|status> [--port 8765] [--host 0.0.0.0]

数据源固定为实时行情（新浪/腾讯/东方财富），不提供任何模拟数据模式。
"""
import argparse
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _print_progress(title):
    def rep(msg, frac=None):
        if frac is not None:
            sys.stdout.write("\r[%s %3.0f%%] %s   " % (title, frac * 100, msg))
            sys.stdout.flush()
        else:
            sys.stdout.write("\n%s\n" % msg)
            sys.stdout.flush()
    return rep


def main():
    ap = argparse.ArgumentParser(description="TrendSniper 趋势选股系统（实时行情）")
    ap.add_argument("cmd", nargs="?", default="serve",
                    choices=["serve", "screen", "backtest", "tune", "status"])
    ap.add_argument("--combos", type=int, default=16)
    ap.add_argument("--host", default=os.environ.get("TRENDSNIPER_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TRENDSNIPER_PORT", "8765")))
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from . import config, jobs, server, store

    store.init_db()
    if not os.path.exists(config.PARAMS_PATH):
        config.save_params(config.default_params())

    if args.cmd == "serve":
        server.serve(host=args.host, port=args.port)
    elif args.cmd == "screen":
        res = jobs.run_screen(progress=_print_progress("选股"), force=True)
        print("\n入选 %d 只：%s" % (
            len(res.get("picks", [])),
            "、".join("%s(%s)" % (p["name"], p["code"]) for p in res.get("picks", []))))
    elif args.cmd == "backtest":
        r = jobs.run_backtest(progress=_print_progress("回测"))
        s = r["report"]["stats"]
        print("\n回测 %d 笔：胜率 %.1f%% 盈亏比 %s 期望 %+.2f%%" % (
            s["n_trades"], (s["win_rate"] or 0) * 100,
            ("%.2f" % s["payoff"]) if s["payoff"] is not None else "∞",
            (s["expectancy"] or 0) * 100))
    elif args.cmd == "tune":
        res = jobs.run_tune(progress=_print_progress("自调优"), combos=args.combos)
        print("\n" + res.get("note", ""))
    elif args.cmd == "status":
        from . import provider as pmod
        p = pmod.create_provider("live")
        print("数据源: %s" % p.label)
        picks = store.json_read(config.PICKS_PATH)
        print("最近选股: %s  入选 %d 只" % (
            store.meta_get("last_screen_at") or "无",
            len((picks or {}).get("picks") or [])))


if __name__ == "__main__":
    main()
