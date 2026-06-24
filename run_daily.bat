@echo off
REM ============================================================
REM  TikTok 后台日报 - 每天定时运行（配合 Windows 任务计划程序）
REM  作用：跑 crawl_backend.py 抓取 8 个页面 → 生成 Excel
REM         → 通过 WSL 里的 Hermes 推送到微信
REM  日志：每天追加到 logs\daily_YYYYMMDD.log
REM ============================================================

REM 切换到本脚本所在目录（即项目根目录）
cd /d "%~dp0"

REM 用 UTF-8 控制台，避免中文乱码
chcp 65001 >nul

REM 准备日志目录
if not exist "logs" mkdir "logs"
set "LOGFILE=logs\daily_%date:~0,4%%date:~5,2%%date:~8,2%.log"

echo. >> "%LOGFILE%"
echo ================ %date% %time% ================ >> "%LOGFILE%"

REM 运行抓取+推送（python 需在 PATH 中；如用虚拟环境请改成对应的 python 路径）
python crawl_backend.py >> "%LOGFILE%" 2>&1

echo ---- 退出码: %ERRORLEVEL% ---- >> "%LOGFILE%"
