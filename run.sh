#!/usr/bin/env bash
# TrendSniper 启动脚本（Linux / macOS）
# 用法: ./run.sh [--host 0.0.0.0] [--port 8765]
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
echo "============================================================"
echo " TrendSniper 趋势选股系统（实时行情） 正在启动 ..."
echo " 启动后按控制台打印的地址打开页面"
echo "============================================================"
exec "$PY" -m backend.main serve "$@"
