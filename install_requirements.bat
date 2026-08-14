@echo off
setlocal
rem Installs all requirements on the closed network, using ONLY the local
rem .\wheelhouse folder (no internet, no internal mirror needed).
rem
rem This script does NOT assume pip is working. If the interpreter has no pip,
rem it bootstraps one straight out of wheelhouse\pip-*.whl - a wheel is a zip,
rem and python can run pip from inside it:
rem     python wheelhouse\pip-XX.whl\pip install ...
rem
rem To refresh the wheelhouse on an internet-connected PC:
rem   pip download -r requirements.txt -d wheelhouse ^
rem       --python-version 310 --only-binary=:all: --platform win_amd64
rem   (repeat for llm_studio\requirements.txt and spec-reader\requirements.txt)
cd /d "%~dp0"

rem ===== EDIT: internal PyPI mirror (leave blank for offline wheelhouse-only) =====
rem Keep these BLANK in the repository - this file is public, and an internal
rem host/IP does not belong in it. Fill them in on the company PC only.
set "MIRROR_INDEX="
set "MIRROR_HOST="
rem Example:
rem   set "MIRROR_INDEX=http://pypi.company.local/simple"
rem   set "MIRROR_HOST=pypi.company.local"

rem ===== EDIT: also write a per-user pip.ini so the mirror attaches to EVERY pip =====
rem Needs MIRROR_INDEX/MIRROR_HOST above; ignored when they are blank.
set "WRITE_PIP_INI=1"
set "PIPDIR=%USERPROFILE%\pip"

rem ===== EDIT: install into a local venv\ folder (1) or the system python (0) =====
set "USE_VENV=1"

rem ===== EDIT: which requirement sets to install (1/0) =====
rem   MCP servers          -> requirements.txt              (always)
rem   LocalLLM Studio app  -> llm_studio\requirements.txt
rem   spec-reader tools    -> spec-reader\requirements.txt  (openpyxl, requests)
set "INSTALL_LLM_STUDIO=1"
set "INSTALL_SPEC_READER=1"

set "WHEELS=%~dp0wheelhouse"
if not exist "%WHEELS%" goto :no_wheels

rem ===== locate the bundled pip wheel (used only if the interpreter has no pip) =====
set "PIPWHL="
for %%F in ("%WHEELS%\pip-*.whl") do set "PIPWHL=%%~fF"

rem ===== find a python interpreter =====
set "SYSPY="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "SYSPY=python"
if defined SYSPY goto :got_python
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "SYSPY=py -3"
if defined SYSPY goto :got_python
goto :no_python

:got_python
echo Using interpreter: %SYSPY%
%SYSPY% -c "import sys;print('  python',sys.version.split()[0],sys.executable)"

set "PY=%SYSPY%"
if not "%USE_VENV%"=="1" goto :have_py
if exist "venv\Scripts\python.exe" goto :venv_ready

echo Creating virtualenv: %~dp0venv
%SYSPY% -m venv venv
if exist "venv\Scripts\python.exe" goto :venv_ready
echo [INFO] venv creation failed - retrying with --without-pip
echo        (the system python has no ensurepip; pip is bootstrapped below)
%SYSPY% -m venv --without-pip venv
if exist "venv\Scripts\python.exe" goto :venv_ready
goto :no_venv

:venv_ready
set "PY=venv\Scripts\python.exe"

:have_py
rem ===== make sure the target interpreter has a working pip =====
%PY% -m pip --version >nul 2>&1
if not errorlevel 1 goto :pip_ok
echo [INFO] pip is not available in this interpreter - bootstrapping from wheelhouse
if not defined PIPWHL goto :no_pipwhl
echo        %PIPWHL%
%PY% "%PIPWHL%\pip" install --no-index --find-links "%WHEELS%" pip setuptools wheel
%PY% -m pip --version >nul 2>&1
if errorlevel 1 goto :no_pip

:pip_ok
%PY% -m pip --version

rem ===== Write per-user pip.ini so future pip calls hit the mirror automatically =====
if not "%WRITE_PIP_INI%"=="1" goto :skip_ini
if "%MIRROR_INDEX%"=="" goto :skip_ini
if not exist "%PIPDIR%" mkdir "%PIPDIR%"
if exist "%PIPDIR%\pip.ini" (
    echo [SKIP] pip.ini already exists - leaving it as is: %PIPDIR%\pip.ini
) else (
    > "%PIPDIR%\pip.ini" echo [global]
    >>"%PIPDIR%\pip.ini" echo index-url=%MIRROR_INDEX%
    >>"%PIPDIR%\pip.ini" echo trusted-host=%MIRROR_HOST%
    echo [OK] Wrote pip.ini: %PIPDIR%\pip.ini
)
:skip_ini

echo.
call :install_reqs "requirements.txt"
if errorlevel 1 goto :failed

if not "%INSTALL_LLM_STUDIO%"=="1" goto :skip_studio
call :maybe_install "llm_studio\requirements.txt"
if errorlevel 1 goto :failed
:skip_studio

if not "%INSTALL_SPEC_READER%"=="1" goto :skip_spec
call :maybe_install "spec-reader\requirements.txt"
if errorlevel 1 goto :failed
:skip_spec

echo.
echo === Verifying imports ===
%PY% -c "import fastmcp,mcp,fastapi,uvicorn,openai,pypdf; print('  core        OK')"
%PY% -c "import fitz,PIL; print('  vision RAG  OK')" || echo   vision RAG  MISSING - PDF pages cannot be rendered
%PY% -c "import qdrant_client; print('  qdrant      OK')" || echo   qdrant      MISSING - falls back to sqlite vectors
%PY% -c "import openpyxl,requests; print('  spec-reader OK')" || echo   spec-reader MISSING - catalog.py cannot read the xlsx
%PY% -c "import win32com.client; print('  pywin32     OK')" || echo   pywin32     MISSING - all COM tools return a notice only
echo.
echo [OK] Done.
pause
endlocal
exit /b 0

rem ----- installs one requirements file (%~1) -----
:install_reqs
if "%MIRROR_INDEX%"=="" (
    echo Installing OFFLINE from wheelhouse only: %~1
    %PY% -m pip install -r "%~1" --no-index --find-links "%WHEELS%"
) else (
    echo Installing %~1 from mirror %MIRROR_INDEX% + wheelhouse %WHEELS%
    %PY% -m pip install -r "%~1" --find-links "%WHEELS%" -i "%MIRROR_INDEX%" --trusted-host "%MIRROR_HOST%"
)
exit /b %errorlevel%

rem ----- installs %~1 if it exists, otherwise says so and carries on -----
:maybe_install
if not exist "%~1" (
    echo [SKIP] %~1 not found - skipping.
    exit /b 0
)
call :install_reqs "%~1"
exit /b %errorlevel%

:no_wheels
echo [ERROR] wheelhouse folder not found: %WHEELS%
echo The .whl files ship with this repository - if the folder is missing, the
echo download was incomplete. Re-download the repository ZIP and unpack it all.
pause
endlocal
exit /b 1

:no_python
echo [ERROR] No python found. Tried "python" and "py -3".
echo Install Python 3.10 (64-bit) first, or open a prompt where python is on PATH.
pause
endlocal
exit /b 1

:no_venv
echo [ERROR] Could not create the venv even with --without-pip.
echo Set USE_VENV=0 at the top of this file to install into the system python.
pause
endlocal
exit /b 1

:no_pipwhl
echo [ERROR] No pip-*.whl in %WHEELS% and this interpreter has no pip.
echo Download pip on an online PC: pip download pip -d wheelhouse
pause
endlocal
exit /b 1

:no_pip
echo [ERROR] pip bootstrap failed - see messages above.
echo Check that the pip wheel matches this python (needs 3.10 or newer).
pause
endlocal
exit /b 1

:failed
echo.
echo [ERROR] pip install failed - see messages above.
echo If a package is missing from the wheelhouse, download it on an online PC:
echo   pip download ^<name^> -d wheelhouse --python-version 310 --only-binary=:all: --platform win_amd64
pause
endlocal
exit /b 1
