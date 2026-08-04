@echo off
rem Launches the intranet (SharePoint/groupware search) MCP server.
rem Double-click -> runs over HTTP (:8093) so n8n / llm_studio can connect by URL.
rem Any arguments are passed through as-is, e.g.:
rem   run_intranet_server.bat --probe "vacation policy"
rem   run_intranet_server.bat --site https://portal.company.com/sites/team --save-credential
rem Uses the venv\ virtualenv at the repo root if present, otherwise system python.
rem NOTE: set INTRANET_SITE_URL (or --site) first, and register the account once with
rem       --save-credential. Read-only server: it never writes to the portal.
cd /d "%~dp0"
set "PY=python"
if exist "..\venv\Scripts\python.exe" set "PY=..\venv\Scripts\python.exe"
if "%~1"=="" (
    "%PY%" intranet_server.py --transport http
) else (
    "%PY%" intranet_server.py %*
)
pause
