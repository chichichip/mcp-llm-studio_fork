@echo off
rem Launches the standard-part selection MCP server (playbook-driven step runner).
rem Selection playbooks (.md) live in the playbooks\ folder at the repo root.
rem Double-click -> runs over HTTP (:8093) so n8n / llm_studio can connect by URL.
rem Any arguments are passed through as-is, for example:
rem     run_standard_server.bat --transport stdio
rem     run_standard_server.bat --lint          :: check playbooks, then exit
rem     run_standard_server.bat --list          :: list playbooks, then exit
rem Uses the venv\ virtualenv at the repo root if present, otherwise system python.
cd /d "%~dp0"
set "PY=python"
if exist "..\venv\Scripts\python.exe" set "PY=..\venv\Scripts\python.exe"
if "%~1"=="" (
    "%PY%" standard_server.py --transport http
) else (
    "%PY%" standard_server.py %*
)
pause
