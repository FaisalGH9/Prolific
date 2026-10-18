@echo off
setlocal

title Prolific Monitor - Chrome - Awake for 15 Hours

set "PROJECT_DIR=%~dp0"
set "MONITOR_SCRIPT=%PROJECT_DIR%prolific_monitor.py"
set "MONITOR_BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%MONITOR_BROWSER%" set "MONITOR_BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
set "MONITOR_BROWSER_PORT=9223"
set "MONITOR_BROWSER_PROFILE=%PROJECT_DIR%.browser-profile-chrome"
set "AWAKE_SETUP_SCRIPT=%PROJECT_DIR%Set PowerToys Awake 15 Hours.ps1"
set "AWAKE_EXE=%LOCALAPPDATA%\PowerToys\PowerToys.Awake.exe"

if not exist "%AWAKE_EXE%" set "AWAKE_EXE=%ProgramFiles%\PowerToys\PowerToys.Awake.exe"

if not exist "%MONITOR_SCRIPT%" (
    echo ERROR: prolific_monitor.py was not found in:
    echo %PROJECT_DIR%
    pause
    exit /b 1
)

if not exist "%MONITOR_BROWSER%" (
    echo ERROR: Google Chrome was not found. Install Chrome before starting the monitor.
    pause
    exit /b 1
)

if not exist "%AWAKE_EXE%" (
    echo ERROR: PowerToys Awake was not found.
    echo Make sure Microsoft PowerToys is installed.
    pause
    exit /b 1
)

if not exist "%AWAKE_SETUP_SCRIPT%" (
    echo ERROR: The PowerToys Awake setup script was not found.
    pause
    exit /b 1
)

where py.exe >nul 2>&1
if not errorlevel 1 set "PYTHON_LAUNCHER=py.exe"

if not defined PYTHON_LAUNCHER (
    if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" (
        set "PYTHON_LAUNCHER=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
    )
)

if not defined PYTHON_LAUNCHER (
    if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
        set "PYTHON_LAUNCHER=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    )
)

if not defined PYTHON_LAUNCHER (
    where python.exe >nul 2>&1
    if not errorlevel 1 set "PYTHON_LAUNCHER=python.exe"
)

if not defined PYTHON_LAUNCHER (
    echo ERROR: Python was not found.
    echo Install Python or make sure the py command is available.
    pause
    exit /b 1
)

if /i "%~1"=="--check" goto check_launcher

echo Setting PowerToys Awake for 15 hours with the screen kept on...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%AWAKE_SETUP_SCRIPT%"
if errorlevel 1 goto awake_setup_failed

powershell.exe -NoProfile -Command "$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'prolific_monitor\.py\x22?\s+run-with-browser' }; if ($existing) { exit 10 }"
if errorlevel 10 goto monitor_already_running

cd /d "%PROJECT_DIR%"
echo Starting the Prolific monitor with Google Chrome...
echo.
"%PYTHON_LAUNCHER%" "%MONITOR_SCRIPT%" run-with-browser --browser-path "%MONITOR_BROWSER%" --port %MONITOR_BROWSER_PORT% --profile-dir "%MONITOR_BROWSER_PROFILE%"
set "MONITOR_EXIT_CODE=%ERRORLEVEL%"

echo.
echo The Prolific monitor stopped with exit code %MONITOR_EXIT_CODE%.
pause
exit /b %MONITOR_EXIT_CODE%

:monitor_already_running
echo The Prolific monitor is already running, so another copy was not started.
echo Awake has still been set for 15 hours.
timeout /t 6 /nobreak >nul
exit /b 0

:awake_setup_failed
echo ERROR: PowerToys Awake could not be configured.
echo The Prolific monitor was not started because the PC might go to sleep.
pause
exit /b 1

:check_launcher
echo Launcher file checks passed.
echo Awake: %AWAKE_EXE%
echo Awake setup: %AWAKE_SETUP_SCRIPT%
echo Python: %PYTHON_LAUNCHER%
echo Monitor: %MONITOR_SCRIPT%
echo Browser: %MONITOR_BROWSER%
echo Browser port: %MONITOR_BROWSER_PORT%
echo Browser profile: %MONITOR_BROWSER_PROFILE%
powershell.exe -NoProfile -Command "$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'prolific_monitor\.py\x22?\s+run-with-browser' }; if ($existing) { exit 10 }"
if errorlevel 10 goto check_monitor_found
echo Duplicate-monitor check passed: no running monitor was found.
exit /b 0

:check_monitor_found
echo Duplicate-monitor check passed: an existing monitor was detected.
exit /b 0
