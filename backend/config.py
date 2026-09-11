# -*- coding: utf-8 -*-
"""
全局配置：路径、默认参数、可调参数搜索空间、板块识别、市场代码映射。
策略文本《趋势策略.md》的量化规则均以参数形式落在这里，便于自调优改写。
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
STRATEGY_TEXT = os.path.join(ROOT, "趋势策略.md")
DB_PATH = os.path.join(DATA_DIR, "trendsniper.db")
PARAMS_PATH = os.path.join(DATA_DIR, "strategy_params.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
PICKS_PATH = os.path.join(DATA_DIR, "picks.json")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
TUNE_HISTORY_PATH = os.path.join(DATA_DIR, "tuning_history.json")
FRONTEND_DIR = os.path.join(ROOT, "frontend")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)


def now_ms():
    return int(time.time() * 1000)


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 板块识别（主板 / 创业板 / 科创板 / 北证）
# ---------------------------------------------------------------------------
def classify_board(code):
    """按代码前缀识别 (交易所, 板块)。"""
    code = str(code)
    if code.startswith(("688", "689")):
        return ("sh", "科创板")
    if code.startswith("6"):
        return ("sh", "沪主板")
    if code.startswith(("300", "301")):
        return ("sz", "创业板")
    if code.startswith(("000", "001", "002", "003")):
        return ("sz", "深主板")
    if code.startswith(("43", "83", "87", "88", "92")):
        return ("bj", "北证")
    if code.startswith("8"):
        return ("bj", "北证")
    return ("sz", "深主板")


def exchange_prefix(code):
    """东财 secid 与腾讯行情符号的前缀。"""
    ex, _ = classify_board(code)
    return ex


def em_secid(code):
    """东财 push2 secid: 沪=1.xxx 深/北=0.xxx"""
    ex = exchange_prefix(code)
    return ("1." if ex == "sh" else "0.") + str(code)


def tx_symbol(code):
    ex = exchange_prefix(code)
    return ex + str(code)


def limit_up_pct(board):
    return {"主板": 0.098, "创业板": 0.198, "科创板": 0.198, "北证": 0.298}.get(board, 0.098)


BOARDS = ["沪主板", "深主板", "创业板", "科创板", "北证"]


# ---------------------------------------------------------------------------
# 策略参数默认值  v1.0 —— 全部来自《趋势策略.md》
# ---------------------------------------------------------------------------
DEFAULT_PARAMS = {
    "version": "v1.0",
    "source": "依据《趋势策略.md》手工初值",
    "tuned_at": None,
    "screen": {
        # 全市场全量监控：沪主板 / 深主板 / 创业板 / 科创板 / 北证
        "top_n": 8,                 # 每次入选只数
        "min_price": 3.5,           # 低价股过滤（元）
        "max_price": 300.0,
        "min_float_mv": 20.0,       # 流通市值下限（亿元，默认值）
        "max_float_mv": 2000.0,     # 流通市值上限（亿元，默认值）
        # 按板块差异化流动性门槛：北交所/科创板/创业板体量普遍较小，
        # 使用统一 20 亿会把整个北交所排除在外 → 分别设置合理下限
        "min_float_mv_by_board": {"北证": 2.0, "科创板": 8.0, "创业板": 10.0},
        "max_float_mv_by_board": {"北证": 500.0},
        "exclude_st": True,
        "exclude_nearly_limit_up": True,   # 当日接近涨停不可追
        "min_score": 60.0,          # 加权总分门槛
        "min_history_days": 130,    # 至少要有 120 日线可用
        "auto_relax": True,         # 无人达标时自动放松门槛，保证可操作性
        "max_per_board_ratio": 0.5,  # 单一板块入选占比上限
        "ensure_board_coverage": True,  # 板块保底：有达标票的交易所至少入选1只
        "max_kline_fetch": 0,       # 0 = 全量参与，不抽样（不遗漏任何交易所）
        "per_industry": 0,          # 仅在设置了 max_kline_fetch 时生效
        "fetch_workers": 16,        # K线并发数（全量更新提速）
        "comment": "覆盖全部A股：沪主板/深主板/创业板/科创板/北证",
    },
    "rules": {
        "ma_bull": {
            "short": 20, "mid": 60, "long": 120,
            "mid_rising_days": 5,   # 60日线“趋势向上”判定窗口
            "comment": "当前价>MA20>MA60>MA120，且MA60上行（趋势向上）",
        },
        "new_high": {
            "windows": [60, 120, 250],   # 创近60/120日/历史新高
            "comment": "创阶段新高法：突破前N日最高价",
        },
        "month_ret": {
            "days": 21, "min": 0.12, "max": 0.35,
            "comment": "近一月涨幅适中（原文本15%-30%，自调优可微调）",
        },
        "turnover": {
            "days": 10, "min": 0.015, "max": 0.20,
            "comment": "换手率适中：10日平均换手在区间内",
        },
        "pullback_rising": {
            "recent": 10, "prior": 20,
            "comment": "回调低点抬高：近段回调低点高于前一段回调低点",
        },
        "volume": {
            "up_avg_days": 5, "base_days": 20,
            "mild_up_max": 2.4,      # 上涨平均量 ≤ 基础量×2.4（温和放量）
            "shrink_down_max": 0.9,  # 回调期平均量 ≤ 基础量×0.9（缩量回调）
            "comment": "量价配合：上涨温和放量、回调明显缩量",
        },
        "relative_strength": {
            "bench": "000300", "days": 20, "min": -0.03,
            "comment": "相对强度：20日跑赢沪深300",
        },
        "anti_weak": {
            "days": 40, "down_th": -0.008, "max_down_ratio": 0.55,
            "comment": "大盘大跌时抗跌：大盘下跌日该股下跌占比≤阈值",
        },
        "sector": {
            "days": 20, "min_frac_above_ma60": 0.30, "min_members": 4,
            "window_ret_min": 0.02,
            "comment": "行业景气度：板块内多只站稳MA60+板块近20日上行（板块趋势）",
        },
    },
    "fundamental": {
        # 第二步“基本面筛选”。数据缺失时按加权打分而不硬卡。
        "require": False,
        "min_roe": 8.0,
        "min_np_yoy": 8.0,     # 归母净利同比
        "min_rev_yoy": 0.0,    # 营收同比
        "comment": "高ROE、业绩持续增长/超预期、机构认可",
    },
    "weights": {
        # 各维度权重（归一化打分），自调优会调整它们
        "sector": 0.16,
        "fundamental": 0.13,
        "ma_bull": 0.20,
        "new_high": 0.12,
        "month_ret": 0.08,
        "turnover": 0.04,
        "volume": 0.08,
        "relative_strength": 0.08,
        "anti_weak": 0.05,
        "pullback_rising": 0.06,
    },
    "risk": {
        "stop_pct": 0.08,          # 固定比例止损兜底
        "swing_floor_pct": 0.02,   # 关键支撑(回调低点/MA20)再让 2%
        "timeout_days": 60,        # 持有超过 N 个交易日无趋势则退出
        "trail": "ma20",           # 破位坚决跑：跌破MA20离场
        "comment": "止损=取「买入价×(1-止损%)、MA20×(1-2%)、近期回调低点×(1-2%)」三者最高者",
    },
    "backtest": {
        "history_days": 360,      # 回测考察的历史长度
        "step_days": 4,           # 回测节奏：每 N 个交易日重新做一次选股
        "top_n": 8,
        "cost": 0.0015,           # 单边交易成本+滑点
        "min_trades": 12,         # 样本太少则结果不作数
        "objective_weights": {"win_rate": 0.35, "payoff": 0.30, "expectancy": 0.20, "profit_factor": 0.15},
        "comment": "评价目标：胜率、盈亏比、每笔期望收益、利润因子的加权综合",
    },
    "schedule": {
        # “每天都要实时更新”—— 交易日收盘后自动选股 + 启动补跑
        "screen_weekdays": [0, 1, 2, 3, 4],   # 周一~周五
        "screen_hhmm": "15:20",               # 收盘后(15:00)取当日完整K线选股
        "backtest_weekdays": [4],              # 每周五晚自动回测+自调优
        "backtest_hhmm": "20:30",
        "startup_catchup": True,               # 启动时补跑逾期任务（跨天开机自动更新）
        "comment": "每个交易日收盘后自动刷新选股；停机跨天后启动自动补跑最新交易日",
    },
}

# 自调优可搜索的参数空间（strategy/params 模块使用）
TUNE_SPACE = {
    "rules.month_ret.min": {"type": "float", "lo": 0.06, "hi": 0.20},
    "rules.month_ret.max": {"type": "float", "lo": 0.25, "hi": 0.45},
    "rules.turnover.min": {"type": "float", "lo": 0.005, "hi": 0.03},
    "rules.turnover.max": {"type": "float", "lo": 0.12, "hi": 0.30},
    "rules.ma_bull.mid_rising_days": {"type": "int", "lo": 2, "hi": 10},
    "risk.stop_pct": {"type": "float", "lo": 0.04, "hi": 0.13},
    "risk.timeout_days": {"type": "int", "lo": 30, "hi": 90},
    "screen.min_score": {"type": "float", "lo": 45.0, "hi": 75.0},
    "weights": {"type": "weights", "mutate": 0.15},
    "new_high.windows": {"type": "choice", "options": [[60, 120, 250], [60, 120], [120, 250], [120]]},
}

SECTOR_COLS = ["权重_综合", "止盈止损", "资金", "估值", "业绩", "筹码", "技术", "强度"]


def default_params():
    return json.loads(json.dumps(DEFAULT_PARAMS))


def load_params():
    try:
        with open(PARAMS_PATH, "r", encoding="utf-8") as f:
            p = json.load(f)
        return _merge(default_params(), p)
    except Exception:
        return default_params()


def save_params(p):
    with open(PARAMS_PATH, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def _merge(base, extra):
    """浅合并字典树（extra 覆盖 base），保证新增字段不丢。"""
    out = {}
    for k, v in base.items():
        if isinstance(v, dict) and isinstance(extra.get(k), dict):
            out[k] = _merge(v, extra[k])
        else:
            out[k] = extra.get(k, v)
    for k, v in extra.items():
        if k not in out:
            out[k] = v
    return out


def load_settings():
    d = {"data_source": "live", "auto_refresh": True}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d.update(json.load(f))
    except Exception:
        pass
    return d


def save_settings(d):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
