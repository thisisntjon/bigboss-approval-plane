@echo off
REM Global BigBoss launcher — run `bigboss <command>` from any directory.
REM Self-locating: this script lives in <repo>\scripts, so %~dp0.. is the repo root.
REM State stays repo-pinned (<repo>\.bigboss) regardless of the current directory.
setlocal
set "BIGBOSS_REPO=%~dp0.."
set "PYTHONPATH=%BIGBOSS_REPO%\src"
set "UV_CACHE_DIR=%BIGBOSS_REPO%\.uv-cache"
uv run --project "%BIGBOSS_REPO%" python -m bigboss %*
exit /b %ERRORLEVEL%
