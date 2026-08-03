@echo off
REM LocalLLM Studio exe build script.
REM Prerequisites: pip install -r requirements.txt pyinstaller
REM English/ASCII only - cmd reads this file as cp949, so Korean text is garbled.
REM
REM No .spec file is used on purpose: some corporate transfer/security checks reject
REM .spec (it is a Python script). Every option lives on the command line below, so
REM only .bat and .py files need to cross the boundary. PyInstaller still writes a
REM generated LocalLLMStudio.spec next to this script while building - that one is a
REM local build artifact and is git-ignored.
REM UTF-8 console so unicode in paths/PyInstaller output does not break.
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] PyInstaller build...
REM onedir (folder) layout: onefile unpacks to a temp dir on every launch, which is
REM slow and awkward to ship llama-server + CUDA DLLs alongside.
REM --noupx: UPX-packed exes trip antivirus heuristics far more often.
REM tkinter must NOT be excluded - the native file picker (/api/fs/dialog) imports it
REM inside the function, and the old .spec excluded it, which silently killed that
REM feature in exe builds. Listed as a hidden import too, as belt and braces. On a
REM Python without tk you will see "Hidden import not found" - harmless, the build
REM still completes and exits 0.
pyinstaller --noconfirm --clean ^
  --name LocalLLMStudio ^
  --onedir ^
  --console ^
  --noupx ^
  --add-data "static;static" ^
  --hidden-import tkinter ^
  --hidden-import tkinter.filedialog ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.loops.asyncio ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.http.h11_impl ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on ^
  --hidden-import pypdf ^
  --hidden-import docx ^
  --exclude-module matplotlib ^
  --exclude-module numpy.tests ^
  app.py
if errorlevel 1 (
  echo BUILD FAILED.
  exit /b 1
)

echo [2/2] Checking bundled llama-server...
if exist llama\llama-server.exe (
  xcopy /e /i /y llama dist\LocalLLMStudio\llama >nul
  echo   Copied llama\ into dist.
) else (
  echo   [WARN] llama\llama-server.exe not found.
  echo   Unpack the llama.cpp win-cuda-x64 release into llama\ to ship it
  echo   together with the exe. Without it the app starts idle and you pick
  echo   a model in the UI later.
)

echo.
echo Done: dist\LocalLLMStudio\LocalLLMStudio.exe
echo Next: compile installer.iss with Inno Setup to produce Setup.exe
