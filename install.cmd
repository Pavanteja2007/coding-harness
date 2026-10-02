@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "NEO_REPO_DEFAULT=Pavanteja2007/coding-harness"
set "NEO_REF_DEFAULT=main"
set "PYPI_SPEC=neo-agent-cli"
set "NEO_GIT_PINNED="
if defined NEO_INSTALL_REPO set "NEO_GIT_PINNED=1"
if defined NEO_INSTALL_REF set "NEO_GIT_PINNED=1"
if not defined NEO_INSTALL_REPO set "NEO_INSTALL_REPO=%NEO_REPO_DEFAULT%"
if not defined NEO_INSTALL_REF set "NEO_INSTALL_REF=%NEO_REF_DEFAULT%"
if defined NEO_INSTALL_SOURCE (
    set "SOURCE_URL=%NEO_INSTALL_SOURCE%"
    set "SOURCE_DESC=explicit source"
    goto :source_done
)
if defined NEO_GIT_PINNED (
    set "SOURCE_URL=git+https://github.com/%NEO_INSTALL_REPO%.git@%NEO_INSTALL_REF%"
    set "SOURCE_DESC=github.com/%NEO_INSTALL_REPO% (%NEO_INSTALL_REF%)"
    goto :source_done
)
set "SOURCE_URL=%PYPI_SPEC%"
set "SOURCE_DESC=PyPI (%PYPI_SPEC%, latest)"
:source_done
if defined NEO_INSTALL_SOURCE set "SOURCE_DESC=explicit source: %NEO_INSTALL_SOURCE%"

set "VENV_DIR=%USERPROFILE%\.neo-venv"
set "VENV_SCRIPTS=%VENV_DIR%\Scripts"
set "BIN_DIR=%USERPROFILE%\.neo\bin"
set "ROUTE_MARKER_DIR=%USERPROFILE%\.neo"
set "ROUTE_MARKER_FILE=%ROUTE_MARKER_DIR%\install-route"
set "PATH_MARKER=NEO_INSTALLER_PATH"
set "NEO_PIPX_HOME=%PIPX_HOME%"
if not defined NEO_PIPX_HOME set "NEO_PIPX_HOME=%USERPROFILE%\.local\pipx"

echo.
echo Neo - the AI coding agent for your terminal.
echo Installing from %SOURCE_DESC%...
echo.

set "PY_CMD="
set "PY_ARGS="
set "PY_FOUND="
set "PY_DISPLAY="
set "PY_VER_TEXT="

if defined NEO_PYTHON (
    call :check_python "%NEO_PYTHON%" "%NEO_PYTHON%" ""
    if defined PY_FOUND goto :py_found
)
call :check_python "python" "python" ""
if defined PY_FOUND goto :py_found
where py >nul 2>nul
if not errorlevel 1 call :check_python "py" "py -3" "-3"
if defined PY_FOUND goto :py_found
call :check_python "python3" "python3" ""
if defined PY_FOUND goto :py_found

echo ==^> ERROR: Neo needs Python 3.10-3.12 ^(Windows^). Install it from
echo       https://www.python.org/downloads/ ^(check the
echo       "Add python.exe to PATH" box in the installer^) and re-run
echo       install.cmd.
echo.
echo       Or, with winget:
echo         winget install -e --id Python.Python.3.12
echo.
exit /b 1

:py_found
echo ==^> Found Python: %PY_DISPLAY% ^(%PY_VER_TEXT%^)

echo "%SOURCE_URL%" | findstr /b /l /c:"git+" >nul
if not errorlevel 1 goto :need_git
where git >nul 2>nul
if not errorlevel 1 goto :git_ok
echo ==^> WARNING: git is not installed - fine for PyPI installs, but needed
echo       if you ever pin NEO_INSTALL_REPO/_REF to a git checkout.
goto :git_ok
:need_git
where git >nul 2>nul
if not errorlevel 1 goto :git_ok
echo ==^> ERROR: git is required for git-URL installs ^(source: %SOURCE_URL%^).
echo       Install it from https://git-scm.com/download/win
echo       ^(or: winget install -e --id Git.Git^) and re-run install.cmd.
exit /b 1
:git_ok
where docker >nul 2>nul
if errorlevel 1 (
    echo ==^> WARNING: Docker not found - install it for real bug-fixing
    echo       ^(the sandbox + verifier^). See https://docs.docker.com/get-docker/
    goto :docker_done
)
docker info >nul 2>nul
if errorlevel 1 echo ==^> WARNING: Docker is installed but the daemon is not reachable - start it for real bug-fixing ^(the sandbox + verifier^).
:docker_done

set "NEO_STALE_FAILURE="
for /f "delims=" %%P in ('where neo 2^>nul') do call :remove_stale_pip "%%P"
for /f "delims=" %%P in ('where harness 2^>nul') do call :remove_stale_pip "%%P"
set "NEO_STALE_PATH="
if defined NEO_STALE_FAILURE (
    echo ==^> ERROR: could not remove an older pip-installed Neo command that could shadow this installation.
    exit /b 1
)

set "PIPX_CMD="
if not "%NEO_FORCE_VENV%"=="1" for /f "delims=" %%P in ('where pipx 2^>nul') do if not defined PIPX_CMD set "PIPX_CMD=%%P"
set "PIPX_BIN_DIR=%PIPX_BIN_DIR%"
if not defined PIPX_BIN_DIR set "PIPX_BIN_DIR=%USERPROFILE%\.local\bin"

set "INSTALLED_WITH="
if defined PIPX_CMD (
    echo ==^> Installing with pipx ^(isolated, keeps your system Python clean^)...
    "%PIPX_CMD%" install --force "%SOURCE_URL%"
    if errorlevel 1 (
        echo ==^> WARNING: pipx install failed - falling back to a dedicated venv.
    ) else (
        set "INSTALLED_WITH=pipx"
    )
)

if not defined INSTALLED_WITH (
    if not exist "%VENV_SCRIPTS%\python.exe" (
        echo ==^> Creating an isolated virtual environment at %VENV_DIR% ...
        call :create_venv
        if errorlevel 1 (
            echo ==^> ERROR: could not create the virtual environment.
            echo       Re-run with a Python installed from python.org or
            echo       via "winget install Python.Python.3.12".
            exit /b 1
        )
    ) else (
        echo ==^> Reusing the existing virtual environment at %VENV_DIR% ...
    )
    echo ==^> Installing Neo ^(this may take a minute - dependencies are built on first install^)...
    "%VENV_SCRIPTS%\python.exe" -m pip install --quiet --upgrade pip
    if errorlevel 1 (
        echo ==^> ERROR: pip self-upgrade failed - see the messages above.
        exit /b 1
    )
    "%VENV_SCRIPTS%\python.exe" -m pip install --quiet --upgrade "%SOURCE_URL%"
    if errorlevel 1 (
        echo ==^> ERROR: pip install failed - see the messages above.
        exit /b 1
    )
    set "INSTALLED_WITH=venv"
)

if "%INSTALLED_WITH%"=="pipx" (
    for /f "usebackq delims=" %%D in (`"%PIPX_CMD%" environment --value PIPX_BIN_DIR 2^>nul`) do set "PIPX_BIN_DIR=%%D"
)

if "%INSTALLED_WITH%"=="pipx" (
    set "NEO_BIN=%PIPX_BIN_DIR%\neo.exe"
    set "HARNESS_BIN=%PIPX_BIN_DIR%\harness.exe"
) else (
    if not exist "%VENV_SCRIPTS%\neo.exe" (
        echo ==^> ERROR: installation finished but neo.exe was not found in
        echo       %VENV_SCRIPTS%.
        exit /b 1
    )
    if not exist "%VENV_SCRIPTS%\harness.exe" (
        echo ==^> ERROR: installation finished but harness.exe was not found in
        echo       %VENV_SCRIPTS%.
        exit /b 1
    )
    if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"
    if errorlevel 1 (
        echo ==^> ERROR: could not create %BIN_DIR%.
        exit /b 1
    )
    copy /y "%VENV_SCRIPTS%\neo.exe" "%BIN_DIR%\neo.exe" >nul
    if errorlevel 1 (
        echo ==^> ERROR: could not stage neo.exe into %BIN_DIR%.
        exit /b 1
    )
    copy /y "%VENV_SCRIPTS%\harness.exe" "%BIN_DIR%\harness.exe" >nul
    if errorlevel 1 (
        echo ==^> ERROR: could not stage harness.exe into %BIN_DIR%.
        exit /b 1
    )
    set "NEO_BIN=%BIN_DIR%\neo.exe"
    set "HARNESS_BIN=%BIN_DIR%\harness.exe"
)

if not exist "%NEO_BIN%" (
    echo ==^> ERROR: installation finished but neo.exe was not found at
    echo       %NEO_BIN%.
    exit /b 1
)
if not exist "%HARNESS_BIN%" (
    echo ==^> ERROR: installation finished but harness.exe was not found at
    echo       %HARNESS_BIN%.
    exit /b 1
)

set "NEEDS_PATH=%BIN_DIR%"
if "%INSTALLED_WITH%"=="pipx" set "NEEDS_PATH=%PIPX_BIN_DIR%"

set "NEO_PERSIST_REMOVE=%BIN_DIR%;%VENV_SCRIPTS%;%VENV_DIR%\bin"

set "PREVIOUS_ROUTE="
set "PREVIOUS_ROUTE_PATH="
if exist "%ROUTE_MARKER_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%ROUTE_MARKER_FILE%") do (
        if "%%A"=="route" set "PREVIOUS_ROUTE=%%B"
        if "%%A"=="path" set "PREVIOUS_ROUTE_PATH=%%B"
    )
)
if defined PREVIOUS_ROUTE_PATH (
    if /I "%PREVIOUS_ROUTE_PATH%"=="%BIN_DIR%" set "NEO_PERSIST_REMOVE=%NEO_PERSIST_REMOVE%;%PREVIOUS_ROUTE_PATH%"
    if /I "%PREVIOUS_ROUTE_PATH%"=="%VENV_SCRIPTS%" set "NEO_PERSIST_REMOVE=%NEO_PERSIST_REMOVE%;%PREVIOUS_ROUTE_PATH%"
    if /I "%PREVIOUS_ROUTE_PATH%"=="%VENV_DIR%\bin" set "NEO_PERSIST_REMOVE=%NEO_PERSIST_REMOVE%;%PREVIOUS_ROUTE_PATH%"
)

set "NEO_ROUTE_PATH=%NEEDS_PATH%"
set "NEO_PATH_OUTPUT=%TEMP%\neo-path-%RANDOM%%RANDOM%.txt"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ^
  "$route=$env:NEO_ROUTE_PATH; $persist=@($env:NEO_PERSIST_REMOVE -split ';' | ForEach-Object { if ($_){$_.Trim()} }); function N([string]$x) { if ([string]::IsNullOrWhiteSpace($x)) { return }; $x=$x.Trim().Trim([char]34).Replace('/','\'); while ($x.Length -gt 3 -and $x.EndsWith('\')) { $x=$x.Substring(0,$x.Length-1) }; return $x }; function M([string]$p,[string[]]$r) { $a=@(); $s=@{}; $q=N $route; if ($q) { $a += $q; $s[$q.ToLowerInvariant()]=$true }; $bad=@($r | ForEach-Object { N $_ } | Where-Object { $_ } | ForEach-Object { $_.ToLowerInvariant() }); foreach ($x in ($p -split ';')) { $v=N $x; if (-not $v) { continue }; $k=$v.ToLowerInvariant(); if ($k -eq $q.ToLowerInvariant() -or $bad -contains $k -or $s.ContainsKey($k)) { continue }; $a += $v; $s[$k]=$true }; return ($a -join ';') }; $u=[Environment]::GetEnvironmentVariable('Path','User'); $nu=M $u $persist; if ($nu -ne $u) { [Environment]::SetEnvironmentVariable('Path',$nu,'User') }; $stored=[Environment]::GetEnvironmentVariable('Path','User'); if ((M $stored $persist) -ne $nu) { exit 11 }; $machine=[Environment]::GetEnvironmentVariable('Path','Machine'); $fresh="$machine;$stored"; [IO.File]::WriteAllText($env:NEO_PATH_OUTPUT,$fresh)"
if errorlevel 1 (
    echo ==^> ERROR: could not update or verify PATH.
    set "NEO_PATH_OUTPUT="
    exit /b 1
)
if not exist "%NEO_PATH_OUTPUT%" (
    echo ==^> ERROR: PATH normalization returned no result.
    exit /b 1
)
set /p "PATH="<"%NEO_PATH_OUTPUT%"
del /q "%NEO_PATH_OUTPUT%" >nul 2>nul
set "NEO_PATH_OUTPUT="
set "NEO_ROUTE_PATH="
set "NEO_PERSIST_REMOVE="

set "RESOLVED_NEO="
for /f "usebackq delims=" %%P in (`where neo 2^>nul`) do if not defined RESOLVED_NEO set "RESOLVED_NEO=%%P"
set "RESOLVED_HARNESS="
for /f "usebackq delims=" %%P in (`where harness 2^>nul`) do if not defined RESOLVED_HARNESS set "RESOLVED_HARNESS=%%P"
if /I not "%RESOLVED_NEO%"=="%NEO_BIN%" (
    echo ==^> ERROR: neo resolved to "%RESOLVED_NEO%" instead of "%NEO_BIN%".
    exit /b 1
)
if /I not "%RESOLVED_HARNESS%"=="%HARNESS_BIN%" (
    echo ==^> ERROR: harness resolved to "%RESOLVED_HARNESS%" instead of "%HARNESS_BIN%".
    exit /b 1
)

set "NEO_VERSION="
set "NEO_TMPV=%TEMP%\neo-ver-%RANDOM%%RANDOM%.txt"
"%RESOLVED_NEO%" --version >"%NEO_TMPV%" 2>nul
if errorlevel 1 (
    echo ==^> ERROR: neo --version failed.
    del /q "%NEO_TMPV%" >nul 2>nul
    exit /b 1
)
for /f "usebackq delims=" %%v in ("%NEO_TMPV%") do if not defined NEO_VERSION set "NEO_VERSION=%%v"
del /q "%NEO_TMPV%" >nul 2>nul
if not defined NEO_VERSION (
    echo ==^> ERROR: neo --version returned no version.
    exit /b 1
)
set "NEO_VERSION=%NEO_VERSION:neo =%"
if not defined NEO_VERSION (
    echo ==^> ERROR: neo --version returned no version.
    exit /b 1
)
"%RESOLVED_HARNESS%" --version >nul 2>nul
if errorlevel 1 (
    echo ==^> ERROR: harness --version failed.
    exit /b 1
)
pushd "%TEMP%" >nul 2>nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
  "$e=$env:NEO_BIN;$h=$env:HARNESS_BIN;$v=(Get-Command neo -CommandType Application -ErrorAction Stop).Source;$h2=(Get-Command harness -CommandType Application -ErrorAction Stop).Source;if($v -ine $e){exit 51};if($h2 -ine $h){exit 52};& $v --version *> $null;if($LASTEXITCODE -ne 0){exit 53};& $h2 --version *> $null;if($LASTEXITCODE -ne 0){exit 54}"
set "FRESH_CMD_RC=%ERRORLEVEL%"
popd
if not "!FRESH_CMD_RC!"=="0" (
    echo ==^> ERROR: a fresh cmd.exe process did not resolve both commands to the intended Neo install.
    echo       Remove any older global neo/harness pip install that still precedes the user PATH.
    exit /b 1
)
if "%NEO_SKIP_UPDATE_CHECK%"=="1" (
    echo ==^> WARNING: skipping neo update --check because NEO_SKIP_UPDATE_CHECK=1.
    goto :update_check_done
)
"%RESOLVED_NEO%" update --check >nul 2>nul
if not errorlevel 1 goto :update_check_done
if "%NEO_REQUIRE_UPDATE_CHECK%"=="1" (
    echo ==^> ERROR: neo update --check failed.
    exit /b 1
)
echo ==^> WARNING: neo update --check failed; the verified local installation is still complete.
:update_check_done

if not exist "%ROUTE_MARKER_DIR%" mkdir "%ROUTE_MARKER_DIR%"
if errorlevel 1 (
    echo ==^> ERROR: could not create the route marker directory.
    exit /b 1
)
set "ROUTE_MARKER_TMP=%ROUTE_MARKER_FILE%.tmp"
> "%ROUTE_MARKER_TMP%" echo route=%INSTALLED_WITH%
>> "%ROUTE_MARKER_TMP%" echo path=%NEEDS_PATH%
>> "%ROUTE_MARKER_TMP%" echo version=1
move /y "%ROUTE_MARKER_TMP%" "%ROUTE_MARKER_FILE%" >nul
if errorlevel 1 (
    echo ==^> ERROR: could not record the installation route.
    exit /b 1
)

echo ==^> Neo %NEO_VERSION% installed via %INSTALLED_WITH%.
echo       location: %NEO_BIN%
echo       fresh cmd.exe process: both neo and harness resolve to the installed Neo route
echo       caller shell: open a new terminal to pick up the persistent PATH change
if defined PREVIOUS_ROUTE if not "!PREVIOUS_ROUTE!"=="!INSTALLED_WITH!" echo       route transition: !PREVIOUS_ROUTE! -^> !INSTALLED_WITH!
echo.
echo Run neo to get started.
echo Docs: https://github.com/%NEO_INSTALL_REPO%#readme
echo.
endlocal & exit /b 0

:remove_stale_pip
set "NEO_STALE_PATH=%~1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ^
  "$p=$env:NEO_STALE_PATH; if (-not $p) { exit 0 }; $venv=$env:VENV_DIR; $pipx=$env:NEO_PIPX_HOME; if ($p -like ($venv + '*') -or $p -like ($pipx + '*')) { exit 0 }; $scripts=[IO.Path]::GetDirectoryName($p); if ([IO.Path]::GetFileName($scripts) -ine 'Scripts') { exit 0 }; $python=Join-Path ([IO.Path]::GetDirectoryName($scripts)) 'python.exe'; if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { exit 0 }; & $python -m pip show $env:PYPI_SPEC *> $null; if ($LASTEXITCODE -ne 0) { exit 0 }; Write-Host ('Removing the existing Neo pip distribution from ' + $scripts + '...'); & $python -m pip uninstall -y $env:PYPI_SPEC; exit $LASTEXITCODE"
if errorlevel 1 set "NEO_STALE_FAILURE=1"
set "NEO_STALE_PATH="
exit /b 0

:check_python
set "PY_CMD=%~1"
set "PY_DISPLAY=%~2"
set "PY_ARGS=%~3"
set "PY_FOUND="
set "PY_VER_TEXT="
set "PY_MAJ="
set "PY_MIN="
set "PY_TMPV=%TEMP%\neo-py-%RANDOM%%RANDOM%.txt"
if defined PY_ARGS (
    %PY_CMD% %PY_ARGS% -c "import sys; print(sys.version_info[0], sys.version_info[1])" >"%PY_TMPV%" 2>nul
) else (
    "%PY_CMD%" -c "import sys; print(sys.version_info[0], sys.version_info[1])" >"%PY_TMPV%" 2>nul
)
for /f "usebackq tokens=1,2" %%a in ("%PY_TMPV%") do (
    set "PY_MAJ=%%a"
    set "PY_MIN=%%b"
)
del /q "%PY_TMPV%" >nul 2>nul
if not defined PY_MAJ (
    set "PY_CMD="
    exit /b 1
)
if not defined PY_MIN (
    set "PY_CMD="
    exit /b 1
)
if !PY_MAJ! LSS 3 (
    set "PY_CMD="
    exit /b 1
)
if !PY_MAJ! EQU 3 if !PY_MIN! LSS 10 (
    set "PY_CMD="
    exit /b 1
)
if !PY_MAJ! EQU 3 if !PY_MIN! GEQ 13 (
    set "PY_CMD="
    exit /b 1
)
set "PY_VER_TEXT=!PY_MAJ!.!PY_MIN!"
set "PY_FOUND=1"
exit /b 0

:create_venv
if defined PY_ARGS (
    %PY_CMD% %PY_ARGS% -m venv "%VENV_DIR%"
) else (
    "%PY_CMD%" -m venv "%VENV_DIR%"
)
exit /b %ERRORLEVEL%
