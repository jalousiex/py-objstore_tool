@echo off
cd /d "%~dp0"
chcp 65001 >nul

echo [objstore_tool] starting ...
uv run objstore_tool
if errorlevel 1 goto fail
goto end

:fail
echo.
echo [objstore_tool] FAILED to start.
echo   0^) uv not found?  install uv first: https://docs.astral.sh/uv/
echo   1^) index/network trouble?  check %APPDATA%\uv\uv.toml
echo   2^) python 3.11 missing?  run: uv python install 3.11
echo.
pause

:end
