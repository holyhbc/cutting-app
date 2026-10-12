@echo off
REM ============================================================
REM  扫码报工 - 每日同步任务
REM  作用：把 VPS 上的报工/改量写回 SQL Server，并把报表数据推回 VPS
REM  运行：每天一次（默认早上 8:07，含随机延迟避免整点扎堆）
REM ============================================================
setlocal enabledelayedexpansion
cd /d G:\hbc\opencode\webapp

REM ---- 配置（按你的实际情况改）----
set "SCAN_SERVER=https://cut.holyhbc.eu.org"
set "SYNC_TOKEN=在这里填你的同步令牌"
set "HBC_DB=ShintHrmDb-test"
set "CONFIRM_PROD="

REM ---- 日志 ----
if not exist G:\hbc\sync-logs mkdir G:\hbc\sync-logs
set "LOG=G:\hbc\sync-logs\%date:~0,4%%date:~5,2%%date:~8,2%.log"

echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo  开始 %date% %time% >> "%LOG%"

REM ---- 1) 报工写回 + 改量回写 ----
echo [1/2] 报工写回 SQL Server... >> "%LOG%"
python sync_reports.py --server "%SCAN_SERVER%" >> "%LOG%" 2>&1
if errorlevel 1 echo [1/2] 失败，退出码 %errorlevel% >> "%LOG%"

REM ---- 2) 报表数据推回 VPS ----
echo [2/2] 报表数据推送... >> "%LOG%"
python push_reports.py --push "%SCAN_SERVER%" >> "%LOG%" 2>&1
if errorlevel 1 echo [2/2] 失败，退出码 %errorlevel% >> "%LOG%"

echo  结束 %time% >> "%LOG%"
endlocal