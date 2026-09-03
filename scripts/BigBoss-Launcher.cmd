@echo off
REM BigBoss desktop launcher — a menu for the common commands. Self-locating (%~dp0.. = repo root);
REM state stays repo-pinned in <repo>\.bigboss regardless of where this runs.
setlocal
set "BIGBOSS_REPO=%~dp0.."
pushd "%BIGBOSS_REPO%"
set "PYTHONPATH=%BIGBOSS_REPO%\src"
set "UV_CACHE_DIR=%BIGBOSS_REPO%\.uv-cache"

:menu
cls
echo ==================================================
echo                     BIG BOSS
echo ==================================================
echo.
echo   [1]  Dashboard        (local approval plane + portfolio, 127.0.0.1:8787)
echo   [2]  Chat             (talk to Big Boss; /council, /fable, /recall)
echo   [3]  Prioritize       (Council ranks your portfolio -^> approval card)
echo   [4]  Processes        (inventory running MCP servers / orphans)
echo   [5]  Portfolio        (registry classification table)
echo   [6]  Run tests
echo   [0]  Exit
echo.
set /p choice="Choose: "

if "%choice%"=="1" goto dashboard
if "%choice%"=="2" goto chat
if "%choice%"=="3" goto prioritize
if "%choice%"=="4" goto ps
if "%choice%"=="5" goto portfolio
if "%choice%"=="6" goto tests
if "%choice%"=="0" goto done
goto menu

:dashboard
echo Starting the dashboard (Ctrl+C to stop, then you return here)...
uv run --project "%BIGBOSS_REPO%" python -m bigboss serve --port 8787
goto menu

:chat
uv run --project "%BIGBOSS_REPO%" python -m bigboss chat
goto menu

:prioritize
uv run --project "%BIGBOSS_REPO%" python -m bigboss council-prioritize --top 8
echo.
pause
goto menu

:ps
uv run --project "%BIGBOSS_REPO%" python -m bigboss ps
echo.
pause
goto menu

:portfolio
uv run --project "%BIGBOSS_REPO%" python -m bigboss registry-classify
echo.
pause
goto menu

:tests
uv run --project "%BIGBOSS_REPO%" python -m unittest discover -s tests
echo.
pause
goto menu

:done
popd
endlocal
