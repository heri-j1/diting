@echo off
cd /d %~dp0
echo [Diting/1] creating venv...
python -m venv .venv
echo [Diting/2] installing dependencies (10-20 min)...
.venv\Scripts\python -m pip install -r requirements.txt
echo [Diting/3] downloading models (~1.6GB)...
.venv\Scripts\python download_models.py
echo [Diting] setup done. Double-click start.bat to run.
pause
