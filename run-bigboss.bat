@echo off
setlocal enabledelayedexpansion

:: Set environment variables
set "PYTHONPATH=src"
set "UV_CACHE_DIR=%CD%\.uv-cache"

:menu
cls
echo ===================================================
echo               BigBoss Control Desk
echo ===================================================
echo.
echo  [1] Standard LAN Serve (host 0.0.0.0, port 8787)
echo  [2] Public Tunnel Serve (expose securely over WAN)
echo  [3] Run Unit Test Suite
echo  [4] Exit
echo.
echo ===================================================
set /p choice="Enter choice [1-4]: "

if "%choice%"=="1" (
    echo.
    echo Starting BigBoss Standard Serve...
    uv run python -m bigboss serve --host 0.0.0.0 --port 8787
    pause
    goto menu
)
if "%choice%"=="2" (
    echo.
    echo Starting BigBoss Secure Public Tunnel...
    uv run python -m bigboss serve --host 0.0.0.0 --port 8787 --tunnel
    pause
    goto menu
)
if "%choice%"=="3" (
    echo.
    echo Running unit tests...
    uv run python -m unittest discover -s tests
    pause
    goto menu
)
if "%choice%"=="4" (
    exit /b 0
)

goto menu
