@echo off
call "%~dp0scripts\unzip.cmd" %*
exit /b %ERRORLEVEL%
