@echo off
rem DLY AEOS launcher for the Windows Command Prompt (cmd.exe). Usage: aeos <command> [args]
rem PowerShell runs .\aeos.ps1 and macOS/Linux run ./aeos; all three start the same CLI from the
rem repository virtual environment that scripts\bootstrap.ps1 creates. In cmd.exe a .ps1 file is not
rem executed (it opens in an editor), so this file exists for cmd users.
setlocal
set "AEOS_EXE=%~dp0.venv\Scripts\aeos.exe"
if not exist "%AEOS_EXE%" (
  echo Virtual environment not found. Run: powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" 1>&2
  exit /b 1
)
"%AEOS_EXE%" %*
exit /b %ERRORLEVEL%
