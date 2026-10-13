@echo off
rem Windows: starts the monitor with the Python that install.bat put into runtime\.
cd /d "%~dp0"
if not exist runtime\venv\Scripts\python.exe (
  echo The monitor is not installed. Run install.bat first ^(see README.md^).
  pause
  exit /b 1
)
rem supervisor.py runs app.py and keeps it updated from GitHub (see README.md).
runtime\venv\Scripts\python supervisor.py
pause
