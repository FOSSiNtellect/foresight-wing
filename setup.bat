@echo off
rem One-step setup for Foresight Wing: packages, Ollama, models, speech weights, Start-menu shortcut.
rem It asks before every install or download. "setup.bat --check" only reports what is ready.
setlocal
cd /d "%~dp0"
set "PY="
rem The Python you run Foresight Wing with ("python"), else the py launcher. (The Microsoft Store stub fails --version.)
python --version >nul 2>nul && set "PY=python"
if not defined PY py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY goto nopython
%PY% tools\setup.py %*
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%

:nopython
echo Python 3.10 or newer is needed to run Foresight Wing.
echo Install it from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH"),
echo or run:  winget install --id Python.Python.3.12 -e
echo then run setup.bat again.
echo.
pause
exit /b 1
