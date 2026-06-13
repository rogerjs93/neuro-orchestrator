@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ZIP="
set "DEST="

:parse
if "%~1"=="" goto done

if /I "%~1"=="-d" (
  set "DEST=%~2"
  shift
  shift
  goto parse
)

if /I "%~1"=="-q"  (
  shift
  goto parse
)

if /I "%~1"=="-o"  (
  shift
  goto parse
)

if /I "%~1"=="-qo" (
  shift
  goto parse
)

if /I "%~1"=="-oq" (
  shift
  goto parse
)

if not defined ZIP set "ZIP=%~1"
shift
goto parse

:done
if not defined ZIP (
  echo unzip shim: missing zip path 1>&2
  exit /b 1
)

if not defined DEST set "DEST=."

set "ZIP_ARG=%ZIP%"
set "DEST_ARG=%DEST%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$zip=$env:ZIP_ARG; $dest=$env:DEST_ARG; New-Item -ItemType Directory -Path $dest -Force | Out-Null; Expand-Archive -Path $zip -DestinationPath $dest -Force"
exit /b %ERRORLEVEL%
