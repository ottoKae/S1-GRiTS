@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_grits_resolver.ps1" %*
exit /b %ERRORLEVEL%

