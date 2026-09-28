@echo off
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8321 ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1
echo [Diting] stopped.
timeout /t 2 >nul
