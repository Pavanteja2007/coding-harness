# install.ps1 - Vex installer for Windows PowerShell.
#
# One-liner (Task C):
#   irm https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.ps1 | iex
#
# What it does:
#   1. Finds a compatible Python (>= 3.10).
#   2. Installs Vex with pipx if available, else into a dedicated virtual
#      environment (%USERPROFILE%\.vex-venv) and exposes `vex` on PATH via
#      %USERPROFILE%\.vex\bin\vex.exe.
#   3. Adds the bin dir to the USER Path (idempotent, no duplicates).
#   4. Verifies `vex` runs and prints the installed version.
#
# Overridable via environment variables:
#   $env:VEX_INSTALL_REPO    GitHub owner/repo   (Pavanteja2007/coding-harness)
#   $env:VEX_INSTALL_REF     branch/tag/commit   (main)
#   $env:VEX_INSTALL_SOURCE  full pip requirement (default: built from the two above)
#   $env:VEX_PYTHON          python executable   (auto-detected)
#
# Safe to re-run; upgrades in place (pipx --force / pip reinstall).
#
# PS 5.1 compatibility notes (this must run under Windows PowerShell 5.1,
# the `irm | iex` default on stock Windows 10/11):
#   - No $ErrorActionPreference='Stop' + native stderr redirection (5.1
#     turns redirected stderr into terminating ErrorRecords); native
#     commands are checked via $LASTEXITCODE instead.
#   - No nested double quotes inside native -c arguments (5.1 mangles
#     them); the version probe uses a quote-free python snippet.
#   - No ?? / || operators (PS 7 only).

$ProgressPreference = 'SilentlyContinue'

$VexRepo = if ($env:VEX_INSTALL_REPO) { $env:VEX_INSTALL_REPO } else { 'Pavanteja2007/coding-harness' }
$VexRef  = if ($env:VEX_INSTALL_REF)  { $env:VEX_INSTALL_REF }  else { 'main' }
if ($env:VEX_INSTALL_SOURCE) {
    $SourceUrl = $env:VEX_INSTALL_SOURCE
} else {
    $SourceUrl = "git+https://github.com/$VexRepo.git@$VexRef"
}

$VenvDir = Join-Path $env:USERPROFILE '.vex-venv'
$BinDir  = Join-Path $env:USERPROFILE '.vex\bin'

# --- output helpers ---------------------------------------------------------

function Write-Step  { param($Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Ok    { param($Msg) Write-Host "==> $Msg" -ForegroundColor Green }
function Write-Note  { param($Msg) Write-Host "  $Msg" }
function Write-Warn2 { param($Msg) Write-Host "==> WARNING: $Msg" -ForegroundColor Yellow }
function Write-Fail   { param($Msg)
    Write-Host "==> ERROR: $Msg" -ForegroundColor Red
    throw "Vex install failed: $Msg"
}

# --- banner -----------------------------------------------------------------

Write-Host ''
Write-Host 'Vex - the AI harness that fixes bugs.'
Write-Host "Installing from github.com/$VexRepo ($VexRef)..."
Write-Host ''

# --- 1. find a compatible Python -------------------------------------------
# Probe order: VEX_PYTHON env -> python -> python3 -> py -3 ->
# standard python.org install dirs (covers "not on PATH" installs).
# The WindowsApps python stub (Store alias, not a real Python) prints a
# "Python was not found" message that fails the numeric parse below and
# is skipped naturally.

function Test-PythonVersion {
    param([string]$Exe)
    try {
        # Quote-free snippet: PS 5.1 mangles nested double quotes in
        # native arguments, so no -c string may contain them.
        $out = & $Exe -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>$null
        if (-not $out) { return $null }
        $parts = "$out".Trim() -split '\s+'
        if ($parts.Count -lt 2) { return $null }
        $major = 0; $minor = 0
        if (-not [int]::TryParse($parts[0], [ref]$major)) { return $null }
        if (-not [int]::TryParse($parts[1], [ref]$minor)) { return $null }
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
            return "$major.$minor"
        }
        return $null
    } catch { return $null }
}

$py = $null
$pyVersion = $null

$candidates = @()
if ($env:VEX_PYTHON) { $candidates += $env:VEX_PYTHON }
$candidates += @('python', 'python3', 'py -3')

foreach ($candidate in $candidates) {
    $resolved = Get-Command $candidate -ErrorAction SilentlyContinue |
                Where-Object { $_.Source -notmatch 'WindowsApps' } |
                Select-Object -First 1
    if ($resolved) {
        $ver = Test-PythonVersion $candidate
        if ($ver) { $py = $candidate; $pyVersion = $ver; break }
    }
}

# Standard python.org install locations (per-user + all-users), for the
# common "installed without 'Add to PATH'" case. Newest first.
if (-not $py) {
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)}
    )
    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        foreach ($exe in (Get-ChildItem -Path $root -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                          Sort-Object FullName -Descending)) {
            $ver = Test-PythonVersion $exe.FullName
            if ($ver) { $py = $exe.FullName; $pyVersion = $ver; break }
        }
        if ($py) { break }
    }
}

if (-not $py) {
    Write-Fail "Vex needs Python 3.10+ (Windows). Install it from
https://www.python.org/downloads/ (check 'Add python.exe to PATH' in the
installer) and re-run:

irm https://raw.githubusercontent.com/$VexRepo/$VexRef/install.ps1 | iex"
}

Write-Step "Found Python: $py ($pyVersion)"

# --- 1b. git is required (the install source is a git URL) ------------------

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail "git is required (Vex installs from a GitHub repository).
Install it from https://git-scm.com/download/win (or 'winget install -e --id Git.Git') and re-run this installer."
}

# --- 2. install -------------------------------------------------------------

$installedWith = $null

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Step 'Installing with pipx (isolated, keeps your system Python clean)...'
    # --force makes re-runs upgrades instead of 'already installed' errors.
    pipx install --force $SourceUrl
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'pipx install failed - falling back to a dedicated venv.'
    } else {
        $installedWith = 'pipx'
    }
}

if (-not $installedWith) {
    if (-not (Test-Path (Join-Path $VenvDir 'Scripts\Activate.ps1'))) {
        Write-Step "Creating an isolated virtual environment at $VenvDir..."
        & $py -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "could not create the virtual environment.
Re-run with a Python installed from python.org (or 'winget install -e --id Python.Python.3.12')."
        }
    } else {
        Write-Step "Reusing the existing virtual environment at $VenvDir..."
    }
    Write-Step 'Installing Vex (this may take a minute - dependencies build on first install)...'
    # python -m pip: the vendored venv pip only upgrades itself via the
    # module form (bare Scripts\pip.exe refuses: "To modify pip, please
    # run ... -m pip install --upgrade pip"). URL requirements
    # re-resolve on every run, so re-runs upgrade naturally.
    $pyExe = Join-Path $VenvDir 'Scripts\python.exe'
    & $pyExe -m pip install --quiet --upgrade pip *> $null
    & $pyExe -m pip install --quiet $SourceUrl
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'pip install failed - see the messages above.'
    }
    $installedWith = 'venv'
}

# --- 3. locate + expose vex on PATH -----------------------------------------

if ($installedWith -eq 'pipx') {
    $pipxBinDir = pipx environment --value PIPX_BIN_DIR 2>$null
    if (-not $pipxBinDir) { $pipxBinDir = Join-Path $env:USERPROFILE '.local\bin' }
    $vexBin = Join-Path $pipxBinDir 'vex.exe'
} else {
    $vexBin = Join-Path $VenvDir 'Scripts\vex.exe'
}

if (-not (Test-Path $vexBin)) {
    Write-Fail "installation finished but vex.exe was not found at the expected location ($vexBin). Please report this: https://github.com/$VexRepo/issues"
}

# venv route: stage the console-script launcher into a stable bin dir
# (keeps the uninstall story: remove .vex-venv + .vex). The launcher exe
# embeds an absolute path to the venv's python.exe, so copying it is safe.
if ($installedWith -eq 'venv') {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    Copy-Item -Path $vexBin -Destination (Join-Path $BinDir 'vex.exe') -Force
    $vexBin = Join-Path $BinDir 'vex.exe'
}

$needsPath = Split-Path -Parent $vexBin

# Add to the USER Path via the registry API (idempotent, no duplicates;
# never setx, which truncates at 1024 chars).
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$pathWasOnPath = $false
if ($userPath) {
    $pathWasOnPath = ($userPath -split ';') -contains $needsPath
}
if (-not $pathWasOnPath) {
    $newPath = if ($userPath) { "$userPath;$needsPath" } else { $needsPath }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
}

# For the CURRENT session (irm|iex users), make `vex` runnable right away.
if (-not (($env:Path -split ';') -contains $needsPath)) {
    $env:Path = "$needsPath;$env:Path"
}

# --- 4. verify + success banner ---------------------------------------------

$vexVersion = & $vexBin --version 2>$null
if (-not $vexVersion) { $vexVersion = 'unknown' }
# `vex --version` prints "vex 0.1.0"; the banner adds its own prefix.
$vexVersion = "$vexVersion" -replace '^vex\s+', ''

Write-Ok "Vex $vexVersion installed via $installedWith."
Write-Note "location: $vexBin"
if ($pathWasOnPath) {
    Write-Note 'on PATH: yes'
} else {
    Write-Note "Added $needsPath to your user PATH (new terminals will find vex automatically)."
}
Write-Host ''
Write-Host 'Run vex to get started.'
Write-Host "Docs: https://github.com/$VexRepo#readme"
Write-Host ''
