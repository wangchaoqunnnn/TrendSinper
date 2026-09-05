# -*- coding: utf-8 -*-
"""SQLite 持久化：日K缓存、基本面快照、元数据、回测报告、交易与调优记录。"""
import json
import os
import sqlite3
import time

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS bars(
  code TEXT, date TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, amount REAL,
  PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS fundamentals(
  code TEXT PRIMARY KEY, report_date TEXT, roe REAL, np_yoy REAL, rev_yoy REAL,
  eps REAL, update_at INTEGER);
CREATE TABLE IF NOT EXISTS universe(
  code TEXT PRIMARY KEY, name TEXT, board TEXT, industry TEXT, price REAL,
  pct REAL, turnover REAL, pe REAL, pb REAL, total_mv REAL, float_mv REAL,
  update_at INTEGER);
CREATE TABLE IF NOT EXISTS industries(
  code TEXT PRIMARY KEY, name TEXT, update_at INTEGER);
CREATE TABLE IF NOT EXISTS tuning_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, method TEXT,
  params TEXT, stats TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT, status TEXT, started INTEGER, finished INTEGER,
  progress REAL, message TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS idx_bars_code ON bars(code);
"""


def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ------------------------------ meta ------------------------------
def meta_get(key, default=None):
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else row["value"]
    finally:
        conn.close()


def meta_set(key, value):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()
    finally:
        conn.close()


# ------------------------------ bars ------------------------------
def upsert_bars(code, rows):
    """rows: list[(date, o,h,l,c, v, amount)]"""
    if not rows:
        return
    conn = connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO bars(code,date,o,h,l,c,v,amount) VALUES(?,?,?,?,?,?,?,?)",
            [(code, r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows])
        conn.commit()
    finally:
        conn.close()


def get_bars(code, limit=600):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT date,o,h,l,c,v,amount FROM bars WHERE code=? "
            "ORDER BY date DESC LIMIT ?", (code, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def bar_count(code):
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) n FROM bars WHERE code=?", (code,)).fetchone()["n"]
    finally:
        conn.close()


def codes_with_bars(min_bars=130):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT code, COUNT(*) n FROM bars GROUP BY code HAVING n>=?", (min_bars,)).fetchall()
        return [r["code"] for r in rows]
    finally:
        conn.close()


# --------------------------- fundamentals --------------------------
def save_fundamentals(items):
    """items: list[dict(code, report_date, roe, np_yoy, rev_yoy, eps)]"""
    if not items:
        return
    conn = connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO fundamentals(code,report_date,roe,np_yoy,rev_yoy,eps,update_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [(it["code"], it.get("report_date"), it.get("roe"), it.get("np_yoy"),
              it.get("rev_yoy"), it.get("eps"), int(time.time())) for it in items])
        conn.commit()
    finally:
        conn.close()


def load_fundamentals(codes=None):
    conn = connect()
    try:
        if codes:
            q = ",".join("?" * len(codes))
            rows = conn.execute(
                f"SELECT * FROM fundamentals WHERE code IN ({q})", list(codes)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM fundamentals").fetchall()
        out = {}
        for r in rows:
            out[r["code"]] = {
                "report_date": r["report_date"], "roe": r["roe"], "np_yoy": r["np_yoy"],
                "rev_yoy": r["rev_yoy"], "eps": r["eps"]}
        return out
    finally:
        conn.close()


def fundamental_count():
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) n FROM fundamentals").fetchone()["n"]
    finally:
        conn.close()


# --------------------------- universe ------------------------------
def save_universe(rows):
    if not rows:
        return
    conn = connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO universe(code,name,board,industry,price,pct,turnover,pe,pb,"
            "total_mv,float_mv,update_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["code"], r["name"], r["board"], r.get("industry"), r["price"], r["pct"],
              r["turnover"], r.get("pe"), r.get("pb"), r["total_mv"], r["float_mv"],
              int(time.time())) for r in rows])
        conn.commit()
    finally:
        conn.close()


def load_universe():
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM universe").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------- industries ---------------------------
def save_industries(items):
    """items: list[(code, industry_name)]"""
    if not items:
        return
    conn = connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO industries(code,name,update_at) VALUES(?,?,?)",
            [(c, n, int(time.time())) for c, n in items])
        conn.commit()
    finally:
        conn.close()


def load_industry_map():
    conn = connect()
    try:
        rows = conn.execute("SELECT code,name FROM industries").fetchall()
        return {r["code"]: r["name"] for r in rows}
    finally:
        conn.close()


# --------------------------- tuning / jobs --------------------------
def add_tuning_log(method, params, stats, note=""):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO tuning_log(at,method,params,stats,note) VALUES(?,?,?,?,?)",
            (config.now_str(), method, json.dumps(params, ensure_ascii=False),
             json.dumps(stats, ensure_ascii=False), note))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    finally:
        conn.close()


def load_tuning_logs(limit=30):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tuning_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in reversed(rows):
            out.append({"id": r["id"], "at": r["at"], "method": r["method"],
                        "params": json.loads(r["params"]), "stats": json.loads(r["stats"]),
                        "note": r["note"]})
        return out
    finally:
        conn.close()


def job_save(job):
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jobs(id,kind,status,started,finished,progress,message,result) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (job["id"], job.get("kind"), job["status"], job.get("started"),
             job.get("finished"), job.get("progress", 0), job.get("message", ""),
             json.dumps(job.get("result"), ensure_ascii=False) if job.get("result") else None))
        conn.commit()
    finally:
        conn.close()


def job_get(job_id):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if r is None:
            return None
        j = dict(r)
        j["result"] = json.loads(j["result"]) if j.get("result") else None
        return j
    finally:
        conn.close()


def job_list_recent(limit=10):
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM jobs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------- JSON files -----------------------------
def json_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def json_read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default
