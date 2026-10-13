@echo off
setlocal

rem Windows installer: downloads the project from GitHub and Python into %USERPROFILE%\InternetStability,
rem then installs the requirements into runtime\venv. Download this file on its own and double-click it.
rem Running it again updates the code and the requirements; config.json, data\ and runtime\python stay.

set "ROOT=%USERPROFILE%\InternetStability"
set "REPO_ZIP_URL=https://github.com/Anton-Shvetsov/internet-stability/archive/refs/heads/main.zip"
set "TMP_DIR=%ROOT%\.download"

set "PY_VER=3.12.10"
set "ENV_DIR=%ROOT%\runtime"
set "PY_DIR=%ENV_DIR%\python"
set "VENV_DIR=%ENV_DIR%\venv"
set "PY_ZIP_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.zip"

rem Progress bar off: it slows Invoke-WebRequest down many times. TLS 1.2: older Windows 10 still
rem defaults to TLS 1.0, which GitHub refuses.
set "PS_INIT=$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'"

rem An update overwrites %ROOT%\install.bat, and cmd reads a running script from disk as it goes:
rem when started from there, run a copy instead.
if /i "%~dp0"=="%ROOT%\" (
    copy /y "%~f0" "%TEMP%\internet-stability-install.bat" >nul
    "%TEMP%\internet-stability-install.bat"
)

echo ============================================================================
echo Installing Internet stability monitor
echo Target folder: %ROOT%
echo ============================================================================

if exist "%ROOT%\app.py" (
    echo Existing installation found: updating it. config.json and data are kept.
    echo Close the monitor window first, otherwise updating the requirements can fail.
    pause
)

if not exist "%ROOT%" mkdir "%ROOT%"
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
mkdir "%TMP_DIR%"

echo ============================================================================
echo Downloading the project from GitHub
echo ============================================================================

echo Downloading archive...
powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_INIT%; Invoke-WebRequest -UseBasicParsing -Uri '%REPO_ZIP_URL%' -OutFile '%TMP_DIR%\project.zip'"
if errorlevel 1 goto :download_failed

echo Extracting archive...
powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_INIT%; Expand-Archive -LiteralPath '%TMP_DIR%\project.zip' -DestinationPath '%TMP_DIR%' -Force"
if errorlevel 1 goto :download_failed

rem Copied over the old code: config.json, data and runtime are not in the repository, so they survive.
for /d %%D in ("%TMP_DIR%\*-main") do (
    xcopy "%%D\*" "%ROOT%\" /s /e /y /q >nul
)
rmdir /s /q "%TMP_DIR%"

if not exist "%ROOT%\app.py" (
    echo ERROR: the project was not downloaded
    pause
    exit /b 1
)

if exist "%PY_DIR%\python.exe" (
    "%PY_DIR%\python.exe" --version >nul 2>&1 && goto :venv
)

echo ============================================================================
echo Installing Python %PY_VER%
echo ============================================================================

if exist "%PY_DIR%" rmdir /s /q "%PY_DIR%"
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
mkdir "%PY_DIR%"

echo Downloading Python archive...
powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_INIT%; Invoke-WebRequest -UseBasicParsing -Uri '%PY_ZIP_URL%' -OutFile '%ENV_DIR%\python.zip'"
if errorlevel 1 goto :download_failed

echo Extracting Python...
powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_INIT%; Expand-Archive -LiteralPath '%ENV_DIR%\python.zip' -DestinationPath '%PY_DIR%' -Force"
if exist "%ENV_DIR%\python.zip" del /f /q "%ENV_DIR%\python.zip"

echo Verifying Python...
"%PY_DIR%\python.exe" --version || (
    echo ERROR: Python failed to start
    pause
    exit /b 1
)

:venv
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    "%PY_DIR%\python.exe" -m venv "%VENV_DIR%"
)

echo ============================================================================
echo Installing requirements
echo ============================================================================

"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade -r "%ROOT%\requirements.txt"
if errorlevel 1 (
    echo ERROR: requirements were not installed
    pause
    exit /b 1
)

echo ============================================================================
echo INSTALLATION SUCCESSFUL: %ROOT%
echo Start the monitor with %ROOT%\start.bat
echo If it was already running, restart it to pick up the update.
echo After that it checks GitHub for updates and installs them by itself.
echo ============================================================================
pause
exit /b 0

:download_failed
echo ERROR: download failed. Check the internet connection and run install.bat again.
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
pause
exit /b 1
