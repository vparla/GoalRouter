@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0goalrouter.ps1" %*
exit /b %ERRORLEVEL%
