@echo off
rem Dreamnet launcher for Windows: double-click to start, then use the browser tab it opens.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  echo Setting up Dreamnet for the first time...
  %PY% -m venv .venv || (echo Python 3.10 or newer is needed: https://www.python.org/downloads/ & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if not exist .env (
  copy .env.example .env >nul
  echo Add your Featherless key and a password to .env, save it, then run this file again.
  notepad .env
  exit /b 0
)
start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:8080"
python -m app.main
pause
