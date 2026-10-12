@echo off
REM ============================================================
REM  Cutting QR daily sync
REM  1) write pending reports + approved changes back to SQL Server
REM  2) push report data back to VPS for the mobile reports
REM  NOTE: this file is intentionally ASCII-only.
REM        cmd.exe reads .cmd in the system ANSI codepage (GBK on a
REM        Chinese Windows); UTF-8 Chinese comments get garbled into
REM        bogus commands. Keep this file ASCII to stay safe.
REM ============================================================
setlocal
REM  switch console to UTF-8 so Chinese log text is readable in Notepad
chcp 65001 >nul
cd /d G:\hbc\opencode\webapp

REM ---- settings (edit if needed) ----
set "SCAN_SERVER=https://cut.holyhbc.eu.org"
set "SYNC_TOKEN=Zv1ASD8Qx5-Mmx3cuWhsfzReamlsrCGba02ZQlc1mqs"
set "HBC_DB=ShintHrmDb-test"

REM ---- log ----
if not exist G:\hbc\sync-logs mkdir G:\hbc\sync-logs
set "LOGDIR=G:\hbc\sync-logs"
set "LOG=%LOGDIR%\sync.log"
if exist "%LOG%" move /y "%LOG%" "%LOGDIR%\sync-%date:~8,2%%date:~5,2%.log" >nul 2>&1

echo. >> "%LOG%"
echo ========================================================== >> "%LOG%"
echo  start %date% %time% >> "%LOG%"

REM ---- 1) report + change writeback ----
echo [1/2] writing reports and changes to SQL Server... >> "%LOG%"
python sync_reports.py --server "%SCAN_SERVER%" >> "%LOG%" 2>&1
if errorlevel 1 echo [1/2] FAILED, exit code %errorlevel% >> "%LOG%"

REM ---- 2) push report data back to VPS ----
echo [2/2] pushing report data to VPS... >> "%LOG%"
python push_reports.py --push "%SCAN_SERVER%" >> "%LOG%" 2>&1
if errorlevel 1 echo [2/2] FAILED, exit code %errorlevel% >> "%LOG%"

echo  done %time% >> "%LOG%"
endlocal