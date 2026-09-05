# -*- coding: utf-8 -*-
"""参数读取/改写工具：点路径读写、自调优候选采样、参数中文摘要。"""
import random

from .. import config


def deep_get(params, path, default=None):
    cur = params
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def deep_set(params, path, value):
    parts = path.split(".")
    cur = params
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def normalize_weights(p):
    w = p["weights"]
    s = sum(v for v in w.values() if v and v > 0)
    if s <= 0:
        return
    for k in w:
        w[k] = round(w[k] / s, 4)


def sample_candidate(params, rng=None, space=None):
    """从 TUNE_SPACE 随机生成一版候选参数，返回 (candidate, changed_keys)。"""
    rng = rng or random.Random()
    space = space or config.TUNE_SPACE
    cand = config.default_params()
    # 以当前 params 为基础（含先前调优结果），保留非调优区设置
    _merge_into(cand, params)
    changed = []

    for path, spec in space.items():
        if spec["type"] == "weights":
            w = cand["weights"]
            for k in list(w.keys()):
                if rng.random() < 0.45:
                    w[k] = max(0.02, w[k] * rng.uniform(1 - spec["mutate"], 1 + spec["mutate"]))
            normalize_weights(cand)
            changed.append("weights")
            continue
        if spec["type"] == "float":
            val = round(rng.uniform(spec["lo"], spec["hi"]), 3)
            deep_set(cand, path, val)
            changed.append(path)
        elif spec["type"] == "int":
            val = rng.randint(spec["lo"], spec["hi"])
            deep_set(cand, path, val)
            changed.append(path)
        elif spec["type"] == "choice":
            val = rng.choice(spec["options"])
            deep_set(cand, path, val)
            changed.append(path)
    return cand, changed


def _merge_into(base, extra):
    """把 extra 中已有键的值合并进 base（保留 base 结构默认）。"""
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge_into(base[k], v)
        elif k in base:
            base[k] = v


def param_summary(p):
    """把当前关键参数渲染成中文一句话摘要。"""
    r = p["rules"]
    s = p["screen"]
    rk = p["risk"]
    nh = "/".join(str(w) for w in r["new_high"]["windows"])
    return (
        "板块信号:创近%s日新高 | 月涨幅 %.0f%%~%.0f%% | 10日均换手 %.1f%%~%.1f%% | "
        "多头排列(价>MA20>MA60>MA120) | 回调低点抬高 | 量价配合 | 相对强度≥%.0f%% | "
        "止损 买入价回撤%.0f%% | 评分门槛 %.0f 分 | 入选 %d 只" % (
            nh, r["month_ret"]["min"] * 100, r["month_ret"]["max"] * 100,
            r["turnover"]["min"] * 100, r["turnover"]["max"] * 100,
            r["relative_strength"]["min"] * 100, rk["stop_pct"] * 100,
            s["min_score"], s["top_n"]))
