@echo off
rem Windows: starts the monitor with the Python that install.bat put into runtime\.
cd /d "%~dp0"
if not exist runtime\venv\Scripts\python.exe (
  echo The monitor is not installed. Run install.bat first ^(see README.md^).
  pause
  exit /b 1
)
runtime\venv\Scripts\python app.py
pause
