# -*- coding: utf-8 -*-
"""极简 HTTP 服务：静态前端 + JSON API + 后台任务线程管理（仅标准库）。"""
import json
import os
import re
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config, jobs, store
from . import provider as provider_mod

ROUTES = []


def route(path_re, methods=("GET",)):
    def deco(fn):
        ROUTES.append((re.compile(path_re), methods, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------
class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._threads = {}

    def start(self, kind, fn, name=None):
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": kind, "status": "running", "started": config.now_ms(),
               "finished": None, "progress": 0.0, "message": "排队中…", "result": None,
               "label": name or kind}
        store.job_save(job)

        def worker():
            def report(msg, frac=None):
                job["message"] = msg
                if frac is not None:
                    job["progress"] = round(frac, 3)
                store.job_save(job)
            try:
                result = fn(report)
                job.update({"status": "done", "message": "完成", "progress": 1.0,
                            "finished": config.now_ms(), "result": result})
            except Exception as e:  # noqa: BLE001
                job.update({"status": "failed", "message": "%s" % e,
                            "finished": config.now_ms(),
                            "result": {"error": str(e),
                                       "trace": traceback.format_exc()[-2000:]}})
            store.job_save(job)

        t = threading.Thread(target=worker, name="job-" + jid, daemon=True)
        with self._lock:
            self._threads[jid] = t
        t.start()
        return job

    def status(self):
        return store.job_list_recent(10)


JOBS = JobManager()


# ---------------------------------------------------------------------------
# 定时调度（“每隔一段时间自行回测，根据回测结果调整策略”）
# ---------------------------------------------------------------------------
class Scheduler(threading.Thread):
    def __init__(self, manager):
        super().__init__(daemon=True, name="scheduler")
        self.manager = manager
        self.stop_flag = threading.Event()
        self.last_check = 0

    def run(self):
        time.sleep(6)
        self._first_run_catchup()
        while not self.stop_flag.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(45)

    def _first_run_catchup(self):
        """首次启动：没有选股结果时自动补跑“选股→回测”，保证开箱即用。"""
        picks = store.json_read(config.PICKS_PATH)
        if not picks or not picks.get("picks"):
            def first_run(rep):
                res = jobs.run_screen(progress=rep, force=True)
                rep("首次选股完成（%d 只），接着回测…" % len(res.get("picks", [])), 0.6)
                bt = jobs.run_backtest(progress=rep)
                rep("首次回测完成", 1.0)
                return {"picks": len(res.get("picks", [])),
                        "backtest_file": bt.get("report_path")}
            JOBS.start("首次自动选股+回测", first_run)
        else:
            last_bt = jobs.latest_backtest()
            if not last_bt:
                JOBS.start("首次自动回测", lambda rep: jobs.run_backtest(progress=rep))
            else:
                # 逾期补跑：距上次调优超过 8 天则启动时自动“回测+自调优”
                logs = store.load_tuning_logs(1)
                stale = True
                if logs:
                    try:
                        import datetime
                        last_at = datetime.datetime.strptime(logs[0]["at"][:10], "%Y-%m-%d")
                        stale = (datetime.date.today() - last_at.date()).days >= 8
                    except Exception:
                        stale = True
                if stale:
                    JOBS.start("启动补跑：周度回测+自调优",
                               lambda rep: _weekly_cycle(rep))

    def _tick(self):
        params = config.load_params()
        sc = params["schedule"]
        now = time.localtime()
        wd = now.tm_wday
        hm = "%02d:%02d" % (now.tm_hour, now.tm_min)
        today = time.strftime("%Y-%m-%d")

        # 每日收盘后选股
        if wd in sc.get("screen_weekdays", []) and hm >= sc.get("screen_hhmm", "18:10"):
            key = "sched:screen:" + today
            if store.meta_get(key, "") != "1":
                store.meta_set(key, "1")
                JOBS.start("定时每日选股", lambda rep: jobs.run_screen(progress=rep))

        # 每周固定时刻自动回测 + 自调优
        if wd in sc.get("backtest_weekdays", [4]) and hm >= sc.get("backtest_hhmm", "20:30"):
            key = "sched:weekly:" + today
            if store.meta_get(key, "") != "1":
                store.meta_set(key, "1")
                JOBS.start("定时每周回测+自调优", lambda rep: _weekly_cycle(rep))


def _weekly_cycle(rep):
    rep("每周自动回测自调优开始…", 0.02)
    tune_res = jobs.run_tune(progress=rep, combos=16)
    rep("调优结束，重跑全样本回测存档…", 0.85)
    bt = jobs.run_backtest(progress=rep)
    rep("每周维护完成", 1.0)
    return {"tune": tune_res, "backtest_file": bt.get("report_path")}


# ---------------------------------------------------------------------------
# API 处理器
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "TrendSniper/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        if self.path.startswith("/api"):
            pass

    # ---------- helpers ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel):
        root = config.FRONTEND_DIR
        path = os.path.normpath(os.path.join(root, rel.lstrip("/")))
        if not path.startswith(root) or not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        ctype = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                 "js": "application/javascript; charset=utf-8", "json": "application/json",
                 "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon",
                 "txt": "text/plain; charset=utf-8"}.get(
                     ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- dispatch ----------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            for pat, methods, fn in ROUTES:
                if pat.match(path) and method in methods:
                    fn(self, path, parse_qs(parsed.query))
                    return
            # 静态资源
            if method == "GET":
                if path == "/" or path == "":
                    self._send_file("index.html")
                else:
                    self._send_file(path.lstrip("/"))
                return
            self._send_json({"error": "no such api"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send_json({"error": str(e)}, 500)
            except Exception:
                pass


# ============================ 状态 ============================
@route(r"^/api/status$")
def api_status(h, path, q):
    settings = config.load_settings()
    params = config.load_params()
    prov = provider_mod.create_provider("live")
    picks = store.json_read(config.PICKS_PATH)
    bt = jobs.latest_backtest()
    paper = jobs.summarize_paper()
    from .strategy import params as pp
    h._send_json({
        "server_time": config.now_str(),
        "data_source": prov.mode, "data_source_label": prov.label,
        "params": {
            "version": params.get("version"), "tuned_at": params.get("tuned_at"),
            "summary": pp.param_summary(params),
        },
        "last_screen": store.meta_get("last_screen_at") or None,
        "screen_date": (picks or {}).get("meta", {}).get("date") if picks else None,
        "picks_count": len((picks or {}).get("picks") or []) if picks else 0,
        "backtest": {
            "period": (bt or {}).get("period"), "n_trades": ((bt or {}).get("stats") or {}).get("n_trades"),
            "win_rate": ((bt or {}).get("stats") or {}).get("win_rate"),
            "payoff": ((bt or {}).get("stats") or {}).get("payoff"),
        } if bt else None,
        "paper": paper.get("stats"),
        "jobs": JOBS.status(),
        "scheduler": "运行中（交易日收盘后选股；每周自动回测+自调优）",
        "settings": settings,
    })


@route(r"^/api/settings$", methods=("POST",))
def api_set_settings(h, path, q):
    # 数据源固定为实时行情；保留接口仅为前端兼容（始终写入 live）
    settings = config.load_settings()
    settings["data_source"] = "live"
    config.save_settings(settings)
    return h._send_json({"ok": True, "mode": "live"})


# ============================ 选股 ============================
@route(r"^/api/picks$")
def api_picks(h, path, q):
    picks = store.json_read(config.PICKS_PATH)
    if not picks:
        h._send_json({"picks": [], "meta": {"empty": True,
                                            "msg": "尚无选股结果，请点击“立即选股”"}})
        return
    # 尝试用最新行情刷新每只票的现价/状态（失败则忽略）
    try:
        prov = provider_mod.create_provider("live")
        codes = [p["code"] for p in picks.get("picks", [])]
        quotes = prov.quote_now(codes)
        for p in picks["picks"]:
            qq = quotes.get(p["code"])
            if qq:
                p["quote"] = qq
                p["status_now"] = ("跌破止损位" if (p["stop_price"] and qq.get("low", 1e18) <= p["stop_price"])
                                   else ("接近止损" if p["stop_price"] and qq["price"] <= p["stop_price"] * 1.03
                                         else "持有观察"))
    except Exception:  # noqa: BLE001
        pass
    h._send_json(picks)


@route(r"^/api/picks/refresh$", methods=("POST",))
def api_refresh_picks(h, path, q):
    body = h._body()
    force = bool(body.get("force"))
    job = JOBS.start("refresh_screen", lambda rep: jobs.run_screen(progress=rep, force=force),
                     name="每日选股")
    h._send_json({"job_id": job["id"], "status": "started"})


# ============================ 回测 ============================
@route(r"^/api/backtest/latest$")
def api_bt_latest(h, path, q):
    bt = jobs.latest_backtest()
    if not bt:
        h._send_json({"none": True})
        return
    bt["trades"] = (bt.get("trades") or [])[:400]
    h._send_json(bt)


@route(r"^/api/backtest/list$")
def api_bt_list(h, path, q):
    h._send_json({"items": jobs.list_backtests(20)})


@route(r"^/api/backtest/run$", methods=("POST",))
def api_bt_run(h, path, q):
    job = JOBS.start("backtest", lambda rep: jobs.run_backtest(progress=rep), name="回测")
    h._send_json({"job_id": job["id"], "status": "started"})


# ============================ 自调优 ============================
@route(r"^/api/tune/history$")
def api_tune_history(h, path, q):
    h._send_json({"items": store.load_tuning_logs(40)})


@route(r"^/api/tune/run$", methods=("POST",))
def api_tune_run(h, path, q):
    body = h._body()
    job = JOBS.start("tune", lambda rep: jobs.run_tune(progress=rep,
                                                       combos=int(body.get("combos", 16))),
                     name="策略自调优")
    h._send_json({"job_id": job["id"], "status": "started"})


# ============================ 参数 ============================
@route(r"^/api/params$")
def api_params(h, path, q):
    h._send_json(config.load_params())


@route(r"^/api/paper$")
def api_paper(h, path, q):
    h._send_json(jobs.summarize_paper())


@route(r"^/api/stats$")
def api_stats(h, path, q):
    """成功率/盈亏比等统计总览：回测 + 虚拟盘。"""
    bt = jobs.latest_backtest()
    paper = jobs.summarize_paper()
    h._send_json({
        "backtest": (bt or {}).get("stats"),
        "paper": paper.get("stats"),
        "by_board": (bt or {}).get("by_board"),
        "by_reason": (bt or {}).get("by_reason"),
        "tuning": store.load_tuning_logs(10),
    })


@route(r"^/api/strategy-text$")
def api_strategy(h, path, q):
    try:
        with open(config.STRATEGY_TEXT, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        text = ""
    h._send_json({"text": text})


@route(r"^/api/jobs$")
def api_jobs(h, path, q):
    h._send_json({"jobs": JOBS.status()})


@route(r"^/api/jobs/([0-9a-f]+)$")
def api_job_one(h, path, q):
    m = re.match(r"^/api/jobs/([0-9a-f]+)$", path)
    j = store.job_get(m.group(1))
    if not j:
        h._send_json({"error": "not found"}, 404)
    else:
        h._send_json(j)


# ---------------------------------------------------------------------------
def serve(host=None, port=None):
    import socket
    host = host or os.environ.get("TRENDSNIPER_HOST", "0.0.0.0")
    port = int(port or os.environ.get("TRENDSNIPER_PORT", "8765"))
    store.init_db()
    config.load_params()          # 确保参数文件存在
    if not os.path.exists(config.PARAMS_PATH):
        config.save_params(config.default_params())
    sched = Scheduler(JOBS)
    sched.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("=" * 64)
    print(" TrendSniper 趋势选股系统（实时行情） 已启动")
    try:
        local = socket.gethostbyname(socket.gethostname())
    except Exception:  # noqa: BLE001
        local = host
    for addr in sorted({host, "127.0.0.1", local}):
        print(" 请打开:  http://%s:%d" % (addr, port))
    print(" 数据源: 新浪 / 腾讯 / 东方财富 实时行情（无任何模拟数据）")
    print(" 定时任务: 交易日收盘后自动选股; 每周自动回测+策略自调优")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
