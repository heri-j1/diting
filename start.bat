@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [Diting] first run: setting up environment, please wait...
  call install.bat
)
netstat -ano | findstr :8321 | findstr LISTENING >nul
if %errorlevel%==0 (
  echo [Diting] already running, opening browser...
  start http://127.0.0.1:8321
  exit /b
)
echo [Diting] starting... browser will open automatically. Log: webapp.log
start "Diting" /min cmd /c ".venv\Scripts\python.exe webapp.py >> webapp.log 2>&1"
timeout /t 3 >nul
