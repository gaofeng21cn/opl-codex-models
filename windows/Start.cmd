@echo off
setlocal
cd /d "%~dp0"
if exist "dist\CodexModelManager\CodexModelManager.exe" (
  start "" "dist\CodexModelManager\CodexModelManager.exe"
  exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "scripts\desktop_entry.py"
  exit /b
)
echo Please download the portable Windows package, or install requirements.txt in .venv first.
pause
