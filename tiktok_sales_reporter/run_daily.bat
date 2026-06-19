@echo off
REM TikTok 销售日报 - Windows 定时任务脚本
REM
REM 用法一（手动运行）：双击此文件
REM
REM 用法二（Windows 定时任务自动每天运行）：
REM   1. 打开"任务计划程序"(Task Scheduler)
REM   2. 创建基本任务 -> 每天 -> 设置时间(如早上9:00)
REM   3. 操作 -> 启动程序 -> 选择此 .bat 文件

cd /d "%~dp0"

echo ========================================
echo TikTok 销售日报任务启动
echo 时间: %date% %time%
echo ========================================

REM 运行 Python 脚本（确保 python 在系统 PATH 中）
python main.py

echo.
echo 任务完成，3秒后关闭窗口...
timeout /t 3
