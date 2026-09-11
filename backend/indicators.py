# -*- coding: utf-8 -*-
"""纯 Python 技术指标工具（无 numpy 依赖），输入按时间升序的列表。"""


def sma(values, n):
    """简单均线，返回与输入等长列表，前 n-1 个为 None。"""
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _rolling(values, n, is_max=True):
    """单调队列实现的滑动极值 O(n)，返回与输入等长列表（前 n-1 个为 None）。"""
    from collections import deque
    out = [None] * len(values)
    dq = deque()     # 存下标，值单调递减(求max) / 递增(求min)
    for i, v in enumerate(values):
        while dq and ((values[dq[-1]] <= v) if is_max else (values[dq[-1]] >= v)):
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = values[dq[0]]
    return out


def rolling_max(values, n):
    return _rolling(values, n, True)


def rolling_min(values, n):
    return _rolling(values, n, False)


def pct_change(values, n):
    out = [None] * len(values)
    for i in range(n, len(values)):
        base = values[i - n]
        if base:
            out[i] = values[i] / base - 1.0
    return out


def ret_between(series, i, k):
    """series[i] 相对 series[i-k] 的涨幅（k 根K线之前）。"""
    if i - k < 0 or not series[i - k]:
        return None
    return series[i] / series[i - k] - 1.0


def arg_last_valid(arr, i=None):
    """在索引 <=i 的范围找最后一个非 None 下标。"""
    if i is None:
        i = len(arr) - 1
    while i >= 0 and arr[i] is None:
        i -= 1
    return i
