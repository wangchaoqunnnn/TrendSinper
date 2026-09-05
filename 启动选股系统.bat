@echo off
chcp 65001 >nul
title TrendSniper 趋势选股系统
cd /d "%~dp0"
echo ============================================================
echo  TrendSniper 趋势选股系统（实时行情） 正在启动...
echo  启动完成后，请按上方控制台打印的地址在浏览器打开
echo  （默认 http://127.0.0.1:8765 ，端口可用环境变量
echo    TRENDSNIPER_PORT 修改）
echo ============================================================
where python >nul 2>nul
if %errorlevel%==0 (
  python -m backend.main serve
) else (
  py -3 -m backend.main serve
)
pause
