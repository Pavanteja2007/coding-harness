@echo off
REM install.cmd - Vex installer for Windows CMD.
REM
REM One-liner (Task C):
REM   curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.cmd -o install.cmd && install.cmd
REM
REM What it does:
REM   1. Finds a compatible Python (3.10+).
REM   2. Installs Vex with pipx if available, else into a dedicated venv
REM      (%USERPROFILE%\.vex-venv) and exposes vex on PATH via
REM      %USERPROFILE%\.vex\bin.
REM   3. Adds the bin dir to the USER Path (idempotent).
REM   4. Verifies vex runs and prints the installed version.
REM
REM Overridable via environment variables:
REM   VEX_INSTALL_REPO, VEX_INSTALL_REF, VEX_INSTALL_SOURCE, VEX_PYTHON
REM
REM Safe to re-run; upgrades in place.
REM NOTE: only `exit /b` is used (never bare `exit`), so running this from
REM an interactive shell never closes the user's terminal.

setlocal EnableExtensions EnableDelayedExpansion

set "VEX_INSTALL_REPO=%VEX_INSTALL_REPO%"
if not defined VEX_INSTALL_REPO set "VEX_INSTALL_REPO=Pavanteja2007/coding-harness"
set "VEX_INSTALL_REF=%VEX_INSTALL_REF%"
if not defined VEX_INSTALL_REF set "VEX_INSTALL_REF=main"
if defined VEX_INSTALL_SOURCE (
    set "SOURCE_URL=%VEX_INSTALL_SOURCE%"
) else (
    set "SOURCE_URL=git+https://github.com/%VEX_INSTALL_REPO%.git@%VEX_INSTALL_REF%"
)

set "VENV_DIR=%USERPROFILE%\.vex-venv"
set "BIN_DIR=%USERPROFILE%\.vex\bin"

echo.
echo Vex - the AI harness that fixes bugs.
echo Installing from github.com/%VEX_INSTALL_REPO% (%VEX_INSTALL_REF%)...
echo.

REM --- 1. find a compatible Python (3.10+) ----------------------------------
REM Candidate order: VEX_PYTHON, python, py -3 (the standard Windows
REM launcher), python3. `py -3` covers python.org installs that did
REM not check "Add to PATH". The version probe parses no text: it
REM walks pip-style (3, 10) tuples via a numeric loop below.

set "PY="
set "PY_DISPLAY="
set "PY_VER_TEXT="

if defined VEX_PYTHON (
    call :check_python "%VEX_PYTHON%" "%VEX_PYTHON%"
    if defined PY goto :py_found
)

call :check_python "python" "python"
if defined PY goto :py_found

where py >nul 2>nul && (
    if not defined PY call :check_python "py -3" "py -3"
)
if defined PY goto :py_found

call :check_python "python3" "python3"
if defined PY goto :py_found

echo ==^> ERROR: Vex needs Python 3.10+ ^(Windows^). Install it from
echo       https://www.python.org/downloads/ ^(check the
echo       "Add python.exe to PATH" box in the installer^) and re-run
echo       install.cmd.
echo.
echo       Or, with winget:
echo         winget install -e --id Python.Python.3.12
echo.
exit /b 1

:py_found
echo ==^> Found Python: %PY_DISPLAY% (%PY_VER_TEXT%)

REM git is required (the install source is a git URL).
where git >nul 2>nul
if not errorlevel 1 goto :git_ok
echo ==^> ERROR: git is required (Vex installs from a GitHub repository).
echo       Install it from https://git-scm.com/download/win
echo       (or: winget install -e --id Git.Git) and re-run install.cmd.
exit /b 1
:git_ok

REM --- 2. install -----------------------------------------------------------

set "INSTALLED_WITH="
where pipx >nul 2>nul
if not errorlevel 1 (
    echo ==^> Installing with pipx ^(isolated, keeps your system Python clean^)...
    call pipx install --force "%SOURCE_URL%"
    if errorlevel 1 (
        echo ==^> WARNING: pipx install failed - falling back to a dedicated venv.
        set "INSTALLED_WITH="
    ) else (
        set "INSTALLED_WITH=pipx"
    )
)

if not defined INSTALLED_WITH (
    if not exist "%VENV_DIR%\Scripts\activate" (
        echo ==^> Creating an isolated virtual environment at %VENV_DIR% ...
        "%PY%" -m venv "%VENV_DIR%"
        if errorlevel 1 (
            echo ==^> ERROR: could not create the virtual environment.
            echo       Re-run with a Python installed from python.org or
            echo       via "winget install Python.Python.3.12".
            exit /b 1
        )
    ) else (
        echo ==^> Reusing the existing virtual environment at %VENV_DIR% ...
    )
    echo ==^> Installing Vex ^(this may take a minute - dependencies are built on first install^)...
    REM python -m pip: the vendored venv pip only upgrades itself via the
    REM module form (bare Scripts\pip.exe refuses). URL requirements
    REM re-resolve on every run, so re-runs upgrade naturally.
    "%VENV_DIR%\Scripts\python.exe" -m pip install --quiet --upgrade pip
    if errorlevel 1 (
        echo ==^> ERROR: pip self-upgrade failed - see the messages above.
        exit /b 1
    )
    "%VENV_DIR%\Scripts\python.exe" -m pip install --quiet "%SOURCE_URL%"
    if errorlevel 1 (
        echo ==^> ERROR: pip install failed - see the messages above.
        exit /b 1
    )
    set "INSTALLED_WITH=venv"
)

REM --- 3. locate + expose vex on PATH ---------------------------------------

if "%INSTALLED_WITH%"=="pipx" (
    if defined PIPX_BIN_DIR (
        set "VEX_BIN=%PIPX_BIN_DIR%\vex.exe"
    ) else (
        set "VEX_BIN=%USERPROFILE%\.local\bin\vex.exe"
    )
) else (
    set "VEX_BIN=%VENV_DIR%\Scripts\vex.exe"
)

if not exist "%VEX_BIN%" (
    echo ==^> ERROR: installation finished but vex.exe was not found at
    echo       the expected location ^(%VEX_BIN%^).
    echo       Please report this: https://github.com/%VEX_INSTALL_REPO%/issues
    exit /b 1
)

REM venv route: stage the console-script launcher into a stable bin dir
REM (uninstall story: remove .vex-venv + .vex). The launcher exe embeds an
REM absolute path to the venv's python.exe, so copying it is safe.
if "%INSTALLED_WITH%"=="venv" (
    if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"
    copy /y "%VEX_BIN%" "%BIN_DIR%\vex.exe" >nul
    if errorlevel 1 (
        echo ==^> ERROR: could not stage vex.exe into %BIN_DIR%.
        exit /b 1
    )
    set "VEX_BIN=%BIN_DIR%\vex.exe"
)

REM Add to USER Path (idempotent) - via PowerShell, the reliable registry
REM API surface (setx truncates PATH at 1024 chars - never use it).
REM Full path to powershell.exe: its dir may not be on PATH in minimal
REM environments.
if "%INSTALLED_WITH%"=="pipx" (
    if defined PIPX_BIN_DIR (
        set "NEEDS_PATH=%PIPX_BIN_DIR%"
    ) else (
        set "NEEDS_PATH=%USERPROFILE%\.local\bin"
    )
) else (
    set "NEEDS_PATH=%BIN_DIR%"
)

set "VEX_NEEDS_PATH=%NEEDS_PATH%"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command ^
  "$bin = $env:VEX_NEEDS_PATH;" ^
  "if (-not $bin) { $bin = Join-Path $env:USERPROFILE '.vex\bin' };" ^
  "$cur = [Environment]::GetEnvironmentVariable('Path','User');" ^
  "$parts = @($cur -split ';' | Where-Object { $_ -ne '' });" ^
  "if ($parts -notcontains $bin) {" ^
  "  [Environment]::SetEnvironmentVariable('Path', ($parts + $bin) -join ';', 'User');" ^
  "}"
set "VEX_NEEDS_PATH="

REM Current-session PATH so `vex` runs immediately after install.
echo ;%PATH%; | findstr /i /c:";%NEEDS_PATH%;" >nul
if errorlevel 1 (
    set "PATH=%NEEDS_PATH%;%PATH%"
)

REM --- 4. verify + success banner -------------------------------------------

set "VEX_VERSION="
set "VEX_TMPV=%TEMP%\vex-ver-%RANDOM%%RANDOM%.txt"
"%VEX_BIN%" --version >"%VEX_TMPV%" 2>nul
for /f "usebackq delims=" %%v in ("%VEX_TMPV%") do set "VEX_VERSION=%%v"
del "%VEX_TMPV%" >nul 2>nul
if not defined VEX_VERSION set "VEX_VERSION=unknown"
REM `vex --version` prints "vex 0.1.0"; strip the program prefix.
set "VEX_VERSION=%VEX_VERSION:vex =%"
if not defined VEX_VERSION set "VEX_VERSION=unknown"

echo ==^> Vex %VEX_VERSION% installed via %INSTALLED_WITH%.
echo       location: %VEX_BIN%
echo       Added %NEEDS_PATH% to your user PATH ^(new terminals will find vex automatically^).
echo.
echo Run vex to get started.
echo Docs: https://github.com/%VEX_INSTALL_REPO%#readme
echo.
endlocal & exit /b 0

REM --- helper: check one Python candidate -----------------------------------
REM Sets PY (command), PY_DISPLAY, PY_VER_TEXT when >= 3.10; clears PY
REM otherwise. for /f cannot run quoted executables (cmd limitation), so
REM the probe runs to a temp file and is read back with usebackq (its
REM file-reading mode) — this handles "py -3" and paths with spaces.
:check_python
set "PY="
set "PY_CMD=%~1"
set "PY_DISPLAY=%~2"
set "PY_VER_TEXT="
set "PY_MAJ="
set "PY_MIN="
set "PY_TMPV=%TEMP%\vex-py-%RANDOM%%RANDOM%.txt"
"%PY_CMD%" -c "import sys; print(sys.version_info[0], sys.version_info[1])" >"%PY_TMPV%" 2>nul
for /f "usebackq tokens=1,2" %%a in ("%PY_TMPV%") do (
    set "PY_MAJ=%%a"
    set "PY_MIN=%%b"
)
del "%PY_TMPV%" >nul 2>nul
if not defined PY_MAJ exit /b 1
if not defined PY_MIN exit /b 1
REM major must be >= 3 ...
if !PY_MAJ! LSS 3 exit /b 1
REM ... and if it IS 3, minor must be >= 10 (any 4+ passes above check)
if !PY_MAJ! EQU 3 if !PY_MIN! LSS 10 exit /b 1
set "PY_VER_TEXT=!PY_MAJ!.!PY_MIN!"
set "PY=%PY_CMD%"
exit /b 0
