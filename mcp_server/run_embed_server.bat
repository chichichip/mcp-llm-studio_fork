@echo off
rem run_embed_server.bat - start the embedding server used by RAG search.
rem
rem Runs llama-server with --embeddings so rag_core can turn text into vectors.
rem Without it search degrades to keyword-only: asking in different words than
rem the document uses finds nothing. KEEP THIS WINDOW OPEN - closing it stops
rem the server. Start it BEFORE the app and before running the indexer.
rem
rem Paths are not written here (this repo is public and would overwrite your
rem edits on every update). Put them in mcp_server\local_settings.py instead:
rem     RAG_EMBED_GGUF   = r"C:\models\embeddinggemma-Q8_0.gguf"
rem     RAG_LLAMA_SERVER = r"C:\llama\llama-server.exe"
rem The port comes from RAG_EMBED_URL, so the server and the search agree.
rem If a path is missing this script looks in a few common folders first and
rem then tells you exactly what to set.
rem
rem Usage: run_embed_server.bat           (defaults: -ngl 0, one slot, 8192 ctx)
rem        run_embed_server.bat -ngl 99   (extra arguments go to llama-server)
setlocal
cd /d "%~dp0"

set "PY=python"
if exist "..\venv\Scripts\python.exe" set "PY=..\venv\Scripts\python.exe"

set "EMBED_GGUF="
set "EMBED_EXE="
set "EMBED_PORT="
set "EMBED_NOTE="
for /f "usebackq delims=" %%L in (`"%PY%" settings.py embed-launch`) do set %%L

if not defined EMBED_OK goto :nopython
if not defined EMBED_EXE goto :missing
if not defined EMBED_GGUF goto :missing

echo Model : %EMBED_GGUF%
echo Server: %EMBED_EXE%
echo Port  : %EMBED_PORT%
echo.
echo Keep this window open. Ctrl+C stops the server.
echo Check it with:  curl http://127.0.0.1:%EMBED_PORT%/v1/models
echo.
"%EMBED_EXE%" -m "%EMBED_GGUF%" --embeddings -ngl 0 --port %EMBED_PORT% -np 1 -c 8192 -b 8192 -ub 8192 %*
goto :end

:missing
echo.
echo [ERROR] Embedding model or llama-server not found.
if defined EMBED_NOTE echo   %EMBED_NOTE%
echo.
echo Add these two lines to mcp_server\local_settings.py and run again:
echo     RAG_EMBED_GGUF   = r"C:\path\to\embeddinggemma-Q8_0.gguf"
echo     RAG_LLAMA_SERVER = r"C:\path\to\llama-server.exe"
echo.

:nopython
echo.
echo [ERROR] Could not run Python, so the paths could not be looked up.
echo   Tried: %PY%
echo.
echo Check, in this order:
echo   1) Run this by hand and read the error:
echo        "%PY%" settings.py embed-launch
echo   2) Is settings.py next to this .bat? (copy the whole mcp_server folder)
echo   3) Is the venv at ..\venv\Scripts\python.exe, or python on PATH?
echo   4) A syntax error in local_settings.py also breaks this.
echo.

:end
pause
