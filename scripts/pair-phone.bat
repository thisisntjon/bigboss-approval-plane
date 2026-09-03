@echo off
cd /d "%~dp0.."
set PYTHONPATH=src
set UV_CACHE_DIR=%CD%\.uv-cache
uv run python -m bigboss pair %*
