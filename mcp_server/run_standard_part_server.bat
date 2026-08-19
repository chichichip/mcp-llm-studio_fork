@echo off
rem Standard-part finder MCP server (read-only).
rem Narrows "what part do I need" down to a family (mid-category + drawing no.)
rem using the guideline RAG index and the standard-parts catalog (xlsx).
rem It does NOT pick a part number - dash selection needs spec reading + verify.
rem Usage: run_standard_part_server.bat                     (HTTP transport, :8094)
rem        run_standard_part_server.bat --transport stdio
rem        run_standard_part_server.bat --status            (check catalog / index)
rem        run_standard_part_server.bat --find "cushioned clamp for tube"
rem Set STD_CATALOG_PATH in mcp_server\local_settings.py first.
rem Uses the venv\ virtualenv at the repo root if present, otherwise system python.
cd /d "%~dp0"
set "PY=python"
if exist "..\venv\Scripts\python.exe" set "PY=..\venv\Scripts\python.exe"
if "%~1"=="" (
    "%PY%" standard_part_server.py --transport http
) else (
    "%PY%" standard_part_server.py %*
)
pause
