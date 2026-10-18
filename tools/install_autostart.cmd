@echo off
rem ============================================================
rem  Recruit mail notifier - autostart manager (cmd wrapper)
rem
rem  Windows blocks .ps1 files by default, so running the script
rem  directly fails with one of:
rem     "... running scripts is disabled"      (ExecutionPolicy = Restricted)
rem     "... is not digitally signed"          (ExecutionPolicy = AllSigned / RemoteSigned)
rem
rem  This wrapper re-invokes it with -ExecutionPolicy Bypass,
rem  which applies to THIS PROCESS ONLY.  It does not change any
rem  machine or user level security setting.
rem
rem  Usage:
rem    tools\install_autostart.cmd                  register + enable
rem    tools\install_autostart.cmd -Status          show status
rem    tools\install_autostart.cmd -Disable         disable (keeps config)
rem    tools\install_autostart.cmd -Enable          re-enable
rem    tools\install_autostart.cmd -Remove          delete the scheduled task
rem    tools\install_autostart.cmd -DelayMinutes 20 custom start delay
rem
rem  Keep this file ASCII-only (see the note in start_daemon.cmd).
rem ============================================================
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
set RC=%ERRORLEVEL%
endlocal & exit /b %RC%