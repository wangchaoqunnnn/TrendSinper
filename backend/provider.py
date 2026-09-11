# -*- coding: utf-8 -*-
"""实时行情数据提供者（LiveProvider）。

数据全部来自真实行情接口（新浪快照 / 腾讯K线与现价 / 东方财富数据中心），
不存在任何模拟/演示数据。为降低页面与选股延迟，本层内置了多级缓存：
  · 全市场快照缓存 300 秒（收盘后数据不变化，避免每次重复翻页拉取）；
  · 个股现价缓存 10 秒（跟踪页快速刷新）；
  · 日K以 SQLite 持久化，命中即不再请求网络；指数在进程内缓存。
"""
import datetime
import threading
import time

from . import config, dataapi, store
from .config import classify_board


def _norm_meta(row):
    """把快照行规整为引擎统一字段（停牌标的保留并标记 suspended）。"""
    _, board = classify_board(row["code"])
    float_mv = row.get("float_mv")
    price = row.get("price") or 0.0
    suspended = bool(row.get("suspended")) or price <= 0
    return {
        "code": row["code"], "name": row.get("name") or row["code"],
        "board": board, "industry": row.get("industry") or "其他",
        "price": price, "pct": row.get("pct") or 0.0,
        "turnover": row.get("turnover") or 0.0,
        "pe": row.get("pe"), "pb": row.get("pb"),
        "total_mv": row.get("total_mv"), "float_mv": float_mv,
        "main_inflow_pct": row.get("main_inflow_pct"),
        "prev_close": row.get("prev_close"), "high": row.get("high"), "low": row.get("low"),
        "suspended": suspended,
        "is_st": bool(row.get("is_st")) or "ST" in str(row.get("name") or "").upper(),
        "is_leader": bool(row.get("is_leader", False)),
        "shares_float": (float_mv / price) if (float_mv and price) else None,
        "fund": row.get("fund"), "extra": row.get("extra") or {},
    }


class LiveProvider:
    mode = "live"
    label = "实时行情（新浪 / 腾讯 / 东方财富）"
    SNAP_TTL = 300          # 全市场快照缓存（秒）
    QUOTE_TTL = 10          # 现价缓存（秒）

    def __init__(self):
        self._lock = threading.Lock()
        self._snap = None
        self._snap_ts = 0.0
        self._quotes = None
        self._quotes_ts = 0.0
        self._index_cache = {}
        self._index_ts = {}

    # ------------------------------------------------------------------
    # 可用性探测
    # ------------------------------------------------------------------
    def check(self):
        try:
            rows = dataapi.fetch_bars("600000", beg="20260101", end="20260401")
            return len(rows) > 0
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 全市场快照（含缓存）
    # ------------------------------------------------------------------
    def snapshot(self):
        now = time.time()
        if self._snap is not None and now - self._snap_ts < self.SNAP_TTL:
            return self._snap
        rows = dataapi.fetch_snapshot()
        ind_map = store.load_industry_map()
        for r in rows:
            r["industry"] = ind_map.get(r["code"]) or r.get("industry") or "其他"
        out = [_norm_meta(r) for r in rows]
        store.save_universe([{
            "code": r["code"], "name": r["name"], "board": r["board"],
            "industry": r["industry"], "price": r["price"] or 0,
            "pct": r.get("pct") or 0, "turnover": r.get("turnover") or 0,
            "pe": r.get("pe"), "pb": r.get("pb"),
            "total_mv": r.get("total_mv"), "float_mv": r.get("float_mv"),
        } for r in out])
        with self._lock:
            self._snap = out
            self._snap_ts = now
        return out

    def universe_cached(self):
        if self._snap is not None:
            return self._snap
        rows = store.load_universe()
        if rows:
            return [_norm_meta(r) for r in rows]
        return self.snapshot()

    # ------------------------------------------------------------------
    # 个股日K（SQLite 持久化缓存 + 按交易日增量更新）
    # ------------------------------------------------------------------
    def bars(self, code, deep=False):
        # 选股最少只需 130 根（MA120）；缓存约 290 根即可满足全部规则（含250日新高窗口）
        need = 620 if deep else 130
        limit = 900 if deep else 340
        market_last = self.today_str()
        cached = store.get_bars(code, limit=limit)
        last_date = cached[-1]["date"] if cached else None
        if last_date is None or last_date < market_last:
            if last_date:
                try:
                    d = datetime.date.fromisoformat(last_date)
                    beg = (d + datetime.timedelta(days=1)).strftime("%Y%m%d")
                except Exception:  # noqa: BLE001
                    beg = self._default_beg(deep)
            else:
                beg = self._default_beg(deep)
            try:
                rows = dataapi.fetch_bars(code, beg=beg, count=(620 if deep else 320))
                store.upsert_bars(code, rows)
                cached = store.get_bars(code, limit=limit)
            except Exception:  # noqa: BLE001
                pass
        if len(cached) >= need:
            return cached
        # 历史不足（新上市等）则拉全量补齐
        try:
            rows = dataapi.fetch_bars(code, beg=self._default_beg(True), count=620)
            store.upsert_bars(code, rows)
        except Exception:  # noqa: BLE001
            pass
        return store.get_bars(code, limit=limit)

    @staticmethod
    def _default_beg(deep):
        """选股约需 300 根K线（520 自然日），回测需要更长历史（900 自然日）。"""
        days = 900 if deep else 520
        return (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d")

    def codes_with_bars(self, min_bars=130):
        return store.codes_with_bars(min_bars)

    # ------------------------------------------------------------------
    # 基准指数（进程内缓存 600 秒，盘中会自动取到最新）
    # ------------------------------------------------------------------
    INDEX_TTL = 600

    def index_bars(self, code="000300"):
        key = "I" + code
        now = time.time()
        # 缓存有效期内直接返回
        if key in self._index_cache and now - self._index_ts.get(key, 0) < self.INDEX_TTL:
            return self._index_cache[key]
        # 过期/缺失 → 联网取最新；失败才回退本地缓存
        try:
            rows = dataapi.fetch_index_bars(code, beg="20230101")
            rows = [{"date": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                     "v": r[5], "amount": r[6]} for r in rows]
            if rows:
                store.upsert_bars(key, [tuple(r[k] for k in
                                              ("date", "o", "h", "l", "c", "v", "amount"))
                                        for r in rows])
                self._index_cache[key] = rows
                self._index_ts[key] = now
                return rows
        except Exception:  # noqa: BLE001
            pass
        rows = store.get_bars(key, limit=900)
        if rows:
            self._index_cache[key] = rows
            self._index_ts[key] = now
        return rows

    # ------------------------------------------------------------------
    # 批量同步“当日K线”（腾讯行情 60 只/请求，用于把每日全量更新从数千次
    # 单票K线请求降到约百次，显著降低延迟与被限流风险）
    # ------------------------------------------------------------------
    def sync_today_bars(self, codes, market_date, progress=None):
        """codes: 候选代码；market_date: YYYY-MM-DD（最新交易日）。返回统计。"""
        if not codes or not market_date:
            return {"checked": 0, "updated": 0}
        ymd = market_date.replace("-", "")
        updated = 0
        checked = 0
        chunk = 60
        total = len(codes)
        for i in range(0, total, chunk):
            part = codes[i:i + chunk]
            try:
                quotes = dataapi.fetch_quotes(part)
            except Exception:  # noqa: BLE001
                quotes = {}
            items = []
            for code, q in quotes.items():
                checked += 1
                if q.get("date") != ymd:
                    continue
                o, h, l, c = q.get("open"), q.get("high"), q.get("low"), q.get("price")
                if not all([o, h, l, c]):
                    continue
                items.append((code, market_date, o, h, l, c,
                              q.get("vol") or 0.0, q.get("amount") or 0.0))
                updated += 1
            if items:
                store.upsert_bars_multi(items)
            if progress and (i // chunk) % 4 == 0:
                progress("批量同步当日K线 %d/%d（腾讯批量行情）" % (
                    min(i + chunk, total), total), None)
            with self._lock:
                self._quotes = quotes
                self._quotes_ts = time.time()
        return {"checked": checked, "updated": updated}

    # ------------------------------------------------------------------
    # 基本面（东方财富最新报告期）
    # ------------------------------------------------------------------
    def fundamentals(self):
        return store.load_fundamentals()

    def refresh_fundamentals(self):
        rd, items = dataapi.fetch_earnings()
        if items:
            store.save_fundamentals(items)
            store.save_industries([(it["code"], it["industry"]) for it in items
                                   if it.get("industry")])
        return {"report_date": rd, "count": len(items)}

    # ------------------------------------------------------------------
    # 批量现价（带短缓存，避免页面高频刷新重复请求）
    # ------------------------------------------------------------------
    def quote_now(self, codes):
        now = time.time()
        if self._quotes is not None and now - self._quotes_ts < self.QUOTE_TTL:
            return {c: q for c, q in self._quotes.items() if c in codes}
        q = dataapi.fetch_quotes(codes)
        with self._lock:
            self._quotes = q
            self._quotes_ts = now
        return q

    def today_str(self):
        """最近交易日（取自沪深300指数最后一根K线）。"""
        bars = self.index_bars()
        return bars[-1]["date"] if bars else ""


# ---------------------------------------------------------------------------
_PROVIDER = None
_PROVIDER_LOCK = threading.Lock()


def create_provider(mode="live"):
    """实时数据提供者全局单例（mode 参数仅为兼容保留，始终返回实时源）。"""
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = LiveProvider()
        return _PROVIDER


def set_mode(mode="live"):
    """切换数据源（始终为实时源，仅清空缓存用于强制重取）。"""
    global _PROVIDER
    with _PROVIDER_LOCK:
        _PROVIDER = LiveProvider()
    return _PROVIDER
