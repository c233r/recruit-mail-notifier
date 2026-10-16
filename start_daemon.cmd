@echo off
rem ============================================================
rem  Recruit mail notifier - autostart launcher
rem
rem  Runs ONCE and exits:
rem    fetch mail since last run -> classify -> apply completion
rem    replies -> send summary -> exit.  It is NOT a daemon.
rem
rem  RETRY: right after logon the network is often not ready yet, so a
rem  run can fail.  On a non-zero exit code this script waits and tries
rem  again, up to MAX_ATTEMPTS times.  A later success is what counts.
rem
rem  Normal log : logs\run.log        (written by the program itself)
rem  Fallback   : logs\autostart.log  (startup / crash output only;
rem                                    deleted when it exceeds 1MB)
rem
rem  NOTE: pythonw.exe is a GUI-subsystem program, so cmd does NOT wait
rem  for it.  "start /wait" is required, otherwise Task Scheduler
rem  mis-reports the result.
rem
rem  IMPORTANT: keep this file ASCII-only and do NOT use "chcp".
rem  Non-ASCII text plus chcp 65001 makes cmd.exe mis-parse the script.
rem ============================================================
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

set MAX_ATTEMPTS=3
set RETRY_SECONDS=181

if not exist "%~dp0logs" mkdir "%~dp0logs"
if exist "%~dp0logs\autostart.log" for %%A in ("%~dp0logs\autostart.log") do if %%~zA GTR 1048576 del "%~dp0logs\autostart.log"

rem -------- Python interpreter --------
rem Default: whatever pythonw.exe is on PATH.
rem If you use a virtualenv or conda env, put its absolute path here, e.g.
rem   set PY="C:\Users\you\miniconda3\envs\mailbot\pythonw.exe"
set PY=pythonw.exe
where pythonw.exe >nul 2>&1 || set PY=python.exe

set /a ATTEMPT=0

:run_attempt
set /a ATTEMPT+=1
start /wait "" %PY% "%~dp0main.py" run >> "%~dp0logs\autostart.log" 2>&1
set RC=%ERRORLEVEL%
if %RC% EQU 0 goto run_done
if %ATTEMPT% GEQ %MAX_ATTEMPTS% goto run_done

rem Ping-based sleep: works with no console and no stdin.
ping -n %RETRY_SECONDS% 127.0.0.1 >nul
goto run_attempt

:run_done
exit /b 0