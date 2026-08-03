@echo off
REM LocalLLM Studio exe build script.
REM Prerequisites: pip install -r requirements.txt pyinstaller
REM English/ASCII only - cmd reads this file as cp949, so Korean text is garbled.
REM UTF-8 console so unicode in paths/PyInstaller output does not break.
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] PyInstaller build...
pyinstaller --noconfirm LocalLLMStudio.spec
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
