$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Continue'

$NeoRepoDefault = 'Pavanteja2007/coding-harness'
$NeoRefDefault = 'main'
$PypiSpec = 'neo-agent-cli'

if ($env:NEO_INSTALL_SOURCE) {
    $SourceUrl = $env:NEO_INSTALL_SOURCE
    $SourceDesc = "explicit source: $SourceUrl"
    $NeoRepo = if ($env:NEO_INSTALL_REPO) { $env:NEO_INSTALL_REPO } else { $NeoRepoDefault }
    $NeoRef = if ($env:NEO_INSTALL_REF) { $env:NEO_INSTALL_REF } else { $NeoRefDefault }
} elseif ($env:NEO_INSTALL_REPO -or $env:NEO_INSTALL_REF) {
    $NeoRepo = if ($env:NEO_INSTALL_REPO) { $env:NEO_INSTALL_REPO } else { $NeoRepoDefault }
    $NeoRef = if ($env:NEO_INSTALL_REF) { $env:NEO_INSTALL_REF } else { $NeoRefDefault }
    $SourceUrl = "git+https://github.com/$NeoRepo.git@$NeoRef"
    $SourceDesc = "github.com/$NeoRepo ($NeoRef)"
} else {
    $NeoRepo = $NeoRepoDefault
    $NeoRef = $NeoRefDefault
    $SourceUrl = $PypiSpec
    $SourceDesc = "PyPI ($PypiSpec, latest)"
}

$VenvDir = Join-Path $env:USERPROFILE '.neo-venv'
$BinDir = Join-Path $env:USERPROFILE '.neo\bin'
$RouteMarkerDir = Join-Path $env:USERPROFILE '.neo'
$RouteMarkerFile = Join-Path $RouteMarkerDir 'install-route'
$PathMarker = 'NEO_INSTALLER_PATH'
$script:NeoStalePipCleanupFailed = $false

function Write-Step { param($Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Ok { param($Msg) Write-Host "==> $Msg" -ForegroundColor Green }
function Write-Note { param($Msg) Write-Host "  $Msg" }
function Write-Warn2 { param($Msg) Write-Host "==> WARNING: $Msg" -ForegroundColor Yellow }
function Write-Fail {
    param($Msg)
    Write-Host "==> ERROR: $Msg" -ForegroundColor Red
    throw "Neo install failed: $Msg"
}

function Normalize-PathEntry {
    param([string]$Entry)
    if ($null -eq $Entry) { return $null }
    $value = $Entry.Trim().Trim('"')
    if ([string]::IsNullOrWhiteSpace($value)) { return $null }
    $value = $value -replace '/', '\'
    while ($value.Length -gt 3 -and $value.EndsWith('\')) {
        $value = $value.Substring(0, $value.Length - 1)
    }
    return $value
}

function Merge-Path {
    param([string]$Current, [string]$Preferred, [string[]]$Remove)
    $parts = @()
    $seen = @{}
    $preferredValue = Normalize-PathEntry $Preferred
    $preferredKey = ''
    if ($preferredValue) {
        $preferredKey = $preferredValue.ToLowerInvariant()
        $parts += $preferredValue
        $seen[$preferredKey] = $true
    }
    $removeValues = @()
    foreach ($entry in @($Remove)) {
        $value = Normalize-PathEntry $entry
        if ($value) { $removeValues += $value.ToLowerInvariant() }
    }
    foreach ($raw in @($Current -split ';')) {
        $value = Normalize-PathEntry $raw
        if (-not $value) { continue }
        $key = $value.ToLowerInvariant()
        if ($preferredKey -and $key -eq $preferredKey) { continue }
        if ($removeValues -contains $key) { continue }
        if ($seen.ContainsKey($key)) { continue }
        $parts += $value
        $seen[$key] = $true
    }
    return ($parts -join ';')
}

function Resolve-ApplicationPath {
    param([string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name)) { return $null }
    try {
        $command = Get-Command -Name $Name -CommandType Application -ErrorAction Stop |
            Where-Object { $_.Source -notmatch 'WindowsApps' } |
            Select-Object -First 1
        if ($command) {
            $path = $command.Source
            if ([string]::IsNullOrWhiteSpace($path)) { $path = $command.Definition }
            if ($path) { return (Normalize-PathEntry $path) }
        }
    } catch { }
    try {
        if (Test-Path -LiteralPath $Name -PathType Leaf) {
            return (Normalize-PathEntry (Resolve-Path -LiteralPath $Name).ProviderPath)
        }
    } catch { }
    return $null
}

function Test-PythonVersion {
    param([string]$Exe, [string[]]$Arguments)
    try {
        $out = & $Exe @Arguments -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>$null
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0 -or -not $out) { return $null }
        $line = "$($out | Select-Object -Last 1)".Trim()
        $parts = $line -split '\s+'
        if ($parts.Count -lt 2) { return $null }
        $major = 0
        $minor = 0
        if (-not [int]::TryParse($parts[0], [ref]$major)) { return $null }
        if (-not [int]::TryParse($parts[1], [ref]$minor)) { return $null }
        if ($major -eq 3 -and $minor -ge 10 -and $minor -le 12) {
            return "$major.$minor"
        }
    } catch { }
    return $null
}

Write-Host ''
Write-Host 'Neo - the AI coding agent for your terminal.'
Write-Host "Installing from $SourceDesc..."
Write-Host ''

$candidates = @(
    [pscustomobject]@{ Exe = $env:NEO_PYTHON; Arguments = @() },
    [pscustomobject]@{ Exe = 'python'; Arguments = @() },
    [pscustomobject]@{ Exe = 'python3'; Arguments = @() },
    [pscustomobject]@{ Exe = 'py'; Arguments = @('-3') }
)
$py = $null
$pyVersion = $null
$pyDisplay = $null
$pyArguments = @()
foreach ($candidate in $candidates) {
    if ([string]::IsNullOrWhiteSpace($candidate.Exe)) { continue }
    $resolved = Resolve-ApplicationPath $candidate.Exe
    if (-not $resolved) { continue }
    $version = Test-PythonVersion -Exe $resolved -Arguments @($candidate.Arguments)
    if ($version) {
        $py = $resolved
        $pyVersion = $version
        $pyDisplay = $candidate.Exe
        $pyArguments = @($candidate.Arguments)
        break
    }
}

if (-not $py) {
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)}
    )
    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        $found = Get-ChildItem -Path $root -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
        foreach ($exe in $found) {
            $version = Test-PythonVersion -Exe $exe.FullName -Arguments @()
            if ($version) {
                $py = (Normalize-PathEntry $exe.FullName)
                $pyVersion = $version
                $pyDisplay = $py
                $pyArguments = @()
                break
            }
        }
        if ($py) { break }
    }
}

function Uninstall-StalePipInstalls {
    $seen = @{}
    $pipxHome = $env:PIPX_HOME
    if ([string]::IsNullOrWhiteSpace($pipxHome)) { $pipxHome = Join-Path $env:USERPROFILE '.local\pipx' }
    $pipxVenvs = Join-Path $pipxHome 'venvs'
    foreach ($name in @('neo', 'harness')) {
        $commands = @(Get-Command -Name $name -CommandType Application -All -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -notmatch 'WindowsApps' })
        foreach ($command in $commands) {
            $commandPath = $command.Source
            if ([string]::IsNullOrWhiteSpace($commandPath)) { $commandPath = $command.Definition }
            if (-not $commandPath) { continue }
            if ($commandPath -like "$VenvDir*" -or $commandPath -like "$pipxVenvs*") { continue }
            $scriptsDir = Split-Path -Parent $commandPath
            if ((Split-Path -Leaf $scriptsDir) -ne 'Scripts') { continue }
            $python = Join-Path (Split-Path -Parent $scriptsDir) 'python.exe'
            if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { continue }
            $python = (Resolve-Path -LiteralPath $python).ProviderPath
            $key = $python.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            & $python -m pip show $PypiSpec *> $null
            if ($LASTEXITCODE -ne 0) { continue }
            Write-Step "Removing the existing Neo pip distribution from $scriptsDir..."
            & $python -m pip uninstall -y $PypiSpec
            if ($LASTEXITCODE -ne 0) {
                $script:NeoStalePipCleanupFailed = $true
                Write-Warn2 "could not remove the existing Neo distribution from $scriptsDir"
            }
        }
    }
}

if (-not $py) {
    Write-Fail "Neo needs Python 3.10-3.12 (Windows). Install it from https://www.python.org/downloads/ (check 'Add python.exe to PATH' in the installer) and re-run: irm https://raw.githubusercontent.com/$NeoRepo/$NeoRef/install.ps1 | iex"
}

Write-Step "Found Python: $pyDisplay ($pyVersion)"

$systemScriptDir = ''
try {
    $systemPython = "$(& $py @pyArguments -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1)".Trim()
    if ($systemPython) { $systemScriptDir = Split-Path -Parent $systemPython }
} catch { }

if ($SourceUrl -like 'git+*') {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Fail "git is required for git-URL installs (source: $SourceUrl). Install it from https://git-scm.com/download/win (or 'winget install -e --id Git.Git') and re-run this installer."
    }
} elseif (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Warn2 'git is not installed - fine for PyPI installs, but needed if you ever pin NEO_INSTALL_REPO/_REF to a git checkout.'
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Warn2 'Docker not found - install it for real bug-fixing (the sandbox + verifier). See https://docs.docker.com/get-docker/'
} else {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'Docker is installed but the daemon is not reachable - start it for real bug-fixing (the sandbox + verifier).'
    }
}

Uninstall-StalePipInstalls
if ($script:NeoStalePipCleanupFailed) {
    Write-Fail 'could not remove an older pip-installed Neo command that could shadow this installation'
}

$previousRoute = ''
$previousRoutePath = ''
if (Test-Path -LiteralPath $RouteMarkerFile) {
    foreach ($line in @(Get-Content -LiteralPath $RouteMarkerFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^route=(.*)$') { $previousRoute = $Matches[1] }
        if ($line -match '^path=(.*)$') { $previousRoutePath = $Matches[1] }
    }
}

$pipxCommand = $null
if ($env:NEO_FORCE_VENV -ne '1') {
    $pipxCommand = Get-Command pipx -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch 'WindowsApps' } |
        Select-Object -First 1
}
$installedWith = ''
if ($pipxCommand) {
    $pipxExe = $pipxCommand.Source
    if ([string]::IsNullOrWhiteSpace($pipxExe)) { $pipxExe = $pipxCommand.Definition }
    Write-Step 'Installing with pipx (isolated, keeps your system Python clean)...'
    & $pipxExe install --force $SourceUrl
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'pipx install failed - falling back to a dedicated venv.'
    } else {
        $installedWith = 'pipx'
    }
}

$venvScripts = Join-Path $VenvDir 'Scripts'
if (-not $installedWith) {
    if (-not (Test-Path -LiteralPath (Join-Path $venvScripts 'python.exe'))) {
        Write-Step "Creating an isolated virtual environment at $VenvDir..."
        if ($pyArguments.Count -gt 0) {
            & $py @pyArguments -m venv $VenvDir
        } else {
            & $py -m venv $VenvDir
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "could not create the virtual environment. Re-run with a Python installed from python.org (or 'winget install -e --id Python.Python.3.12')."
        }
    } else {
        Write-Step "Reusing the existing virtual environment at $VenvDir..."
    }
    Write-Step 'Installing Neo (this may take a minute - dependencies build on first install)...'
    $venvPython = Join-Path $venvScripts 'python.exe'
    & $venvPython -m pip install --quiet --upgrade pip *> $null
    if ($LASTEXITCODE -ne 0) { Write-Fail 'pip self-upgrade failed - see the messages above.' }
    & $venvPython -m pip install --quiet --upgrade $SourceUrl
    if ($LASTEXITCODE -ne 0) { Write-Fail 'pip install failed - see the messages above.' }
    $installedWith = 'venv'
}

$pipxBinDir = $env:PIPX_BIN_DIR
if ([string]::IsNullOrWhiteSpace($pipxBinDir)) {
    $pipxBinDir = Join-Path $env:USERPROFILE '.local\bin'
}
if ($installedWith -eq 'pipx' -and $pipxCommand) {
    $pipxValue = & $pipxExe environment --value PIPX_BIN_DIR 2>$null
    if ($LASTEXITCODE -eq 0 -and $pipxValue) {
        $pipxLine = @($pipxValue | Where-Object { "$_".Trim() } | Select-Object -Last 1)
        if ($pipxLine.Count -gt 0) { $pipxBinDir = "$($pipxLine[0])".Trim() }
    }
}
$pipxBinDir = Normalize-PathEntry $pipxBinDir
if (-not $pipxBinDir) { $pipxBinDir = Join-Path $env:USERPROFILE '.local\bin' }

if ($installedWith -eq 'venv') {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    $neoSource = Join-Path $venvScripts 'neo.exe'
    $harnessSource = Join-Path $venvScripts 'harness.exe'
    if (-not (Test-Path -LiteralPath $neoSource) -or -not (Test-Path -LiteralPath $harnessSource)) {
        Write-Fail "installation finished but neo.exe and harness.exe were not both found in $venvScripts."
    }
    Copy-Item -LiteralPath $neoSource -Destination (Join-Path $BinDir 'neo.exe') -Force
    Copy-Item -LiteralPath $harnessSource -Destination (Join-Path $BinDir 'harness.exe') -Force
    $neoBin = Join-Path $BinDir 'neo.exe'
    $harnessBin = Join-Path $BinDir 'harness.exe'
} else {
    $neoBin = Join-Path $pipxBinDir 'neo.exe'
    $harnessBin = Join-Path $pipxBinDir 'harness.exe'
}

if (-not (Test-Path -LiteralPath $neoBin) -or -not (Test-Path -LiteralPath $harnessBin)) {
    Write-Fail "installation finished but the expected neo.exe and harness.exe were not both found in $pipxBinDir."
}

$venvBinDir = Join-Path $VenvDir 'bin'
$persistRemovePaths = @($BinDir, $venvScripts, $venvBinDir)
$stalePaths = @($persistRemovePaths + @($pipxBinDir))
if ($previousRoutePath) {
    $previousValue = Normalize-PathEntry $previousRoutePath
    foreach ($knownPath in @($BinDir, $venvScripts, $venvBinDir)) {
        $knownValue = Normalize-PathEntry $knownPath
        if ($previousValue -and $previousValue -eq $knownValue) {
            $persistRemovePaths += $previousRoutePath
            $stalePaths += $previousRoutePath
            break
        }
    }
    if ($previousValue -and $previousValue -eq (Normalize-PathEntry $pipxBinDir)) {
        $stalePaths += $previousRoutePath
    }
}

$needsPath = if ($installedWith -eq 'pipx') { $pipxBinDir } else { $BinDir }
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$oldUserParts = @($userPath -split ';' | ForEach-Object { Normalize-PathEntry $_ } | Where-Object { $_ })
$oldUserFirst = ''
if ($oldUserParts.Count -gt 0) { $oldUserFirst = $oldUserParts[0] }
$routeWasPresent = $oldUserParts | Where-Object { (Normalize-PathEntry $_).ToLowerInvariant() -eq (Normalize-PathEntry $needsPath).ToLowerInvariant() }
$newUserPath = Merge-Path -Current $userPath -Preferred $needsPath -Remove $persistRemovePaths
if ($newUserPath -ne $userPath) {
    try {
        [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
    } catch {
        Write-Fail "could not update the user PATH: $($_.Exception.Message)"
    }
    $storedPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $storedNormalized = Merge-Path -Current $storedPath -Preferred $needsPath -Remove $persistRemovePaths
    if ($storedNormalized -ne $newUserPath) {
        Write-Fail 'the user PATH update could not be verified.'
    }
}
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$freshPath = "$machinePath;$newUserPath"
$currentPath = Merge-Path -Current $env:Path -Preferred $needsPath -Remove $stalePaths
$env:Path = $currentPath
$currentFirst = ''
$currentParts = @($currentPath -split ';')
if ($currentParts.Count -gt 0) { $currentFirst = Normalize-PathEntry $currentParts[0] }
$pathOnPath = $currentFirst -and ((Normalize-PathEntry $currentFirst).ToLowerInvariant() -eq (Normalize-PathEntry $needsPath).ToLowerInvariant())

function Resolve-RouteCommand {
    param([string]$Name, [string]$PathValue)
    $oldPath = $env:Path
    try {
        $env:Path = $PathValue
        $command = Get-Command -Name $Name -CommandType Application -ErrorAction Stop |
            Where-Object { $_.Source -notmatch 'WindowsApps' } |
            Select-Object -First 1
        if (-not $command) { return $null }
        $resolved = $command.Source
        if ([string]::IsNullOrWhiteSpace($resolved)) { $resolved = $command.Definition }
        return (Normalize-PathEntry $resolved)
    } finally {
        $env:Path = $oldPath
    }
}

$resolvedNeo = Resolve-RouteCommand -Name 'neo' -PathValue $currentPath
$resolvedHarness = Resolve-RouteCommand -Name 'harness' -PathValue $currentPath
if (-not $resolvedNeo -or ((Normalize-PathEntry $resolvedNeo).ToLowerInvariant() -ne (Normalize-PathEntry $neoBin).ToLowerInvariant())) {
    Write-Fail "neo resolved to $resolvedNeo instead of the intended $neoBin"
}
if (-not $resolvedHarness -or ((Normalize-PathEntry $resolvedHarness).ToLowerInvariant() -ne (Normalize-PathEntry $harnessBin).ToLowerInvariant())) {
    Write-Fail "harness resolved to $resolvedHarness instead of the intended $harnessBin"
}

$oldFreshPath = $env:Path
$oldExpectedNeo = $env:NEO_EXPECTED_NEO_BIN
$oldExpectedHarness = $env:NEO_EXPECTED_HARNESS_BIN
try {
    $env:Path = $freshPath
    $env:NEO_EXPECTED_NEO_BIN = Normalize-PathEntry $neoBin
    $env:NEO_EXPECTED_HARNESS_BIN = Normalize-PathEntry $harnessBin
    $freshScript = @'
$ErrorActionPreference = 'Continue'
$neo = Get-Command -Name neo -CommandType Application -ErrorAction Stop
$harness = Get-Command -Name harness -CommandType Application -ErrorAction Stop
if ($neo.Source.ToLowerInvariant() -ne $env:NEO_EXPECTED_NEO_BIN.ToLowerInvariant()) { exit 21 }
if ($harness.Source.ToLowerInvariant() -ne $env:NEO_EXPECTED_HARNESS_BIN.ToLowerInvariant()) { exit 22 }
& $neo.Source --version *> $null
if ($LASTEXITCODE -ne 0) { exit 23 }
& $harness.Source --version *> $null
if ($LASTEXITCODE -ne 0) { exit 24 }
'@
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
        $windowsPowerShell = 'powershell.exe'
    }
    & $windowsPowerShell -NoProfile -NonInteractive -Command $freshScript
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'a fresh PowerShell process did not resolve both commands to the intended Neo installation. Remove any older global neo/harness pip install that still precedes the user PATH.'
    }
} finally {
    $env:Path = $oldFreshPath
    $env:NEO_EXPECTED_NEO_BIN = $oldExpectedNeo
    $env:NEO_EXPECTED_HARNESS_BIN = $oldExpectedHarness
}

$neoVersionOutput = & $resolvedNeo --version 2>$null
$neoExitCode = $LASTEXITCODE
if ($neoExitCode -ne 0 -or -not $neoVersionOutput) {
    Write-Fail 'neo --version failed through the constructed installation PATH.'
}
$neoVersion = "$($neoVersionOutput | Select-Object -Last 1)".Trim()
$neoVersion = $neoVersion -replace '^neo\s+', ''
if ([string]::IsNullOrWhiteSpace($neoVersion)) { Write-Fail 'neo --version returned no version.' }
& $resolvedHarness --version 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { Write-Fail 'harness --version failed through the constructed installation PATH.' }
if ($env:NEO_SKIP_UPDATE_CHECK -eq '1') {
    Write-Warn2 'skipping neo update --check because NEO_SKIP_UPDATE_CHECK=1'
} else {
    & $resolvedNeo update --check 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($env:NEO_REQUIRE_UPDATE_CHECK -eq '1') {
            Write-Fail 'neo update --check failed through the constructed installation PATH.'
        }
        Write-Warn2 'neo update --check failed; the verified local installation is still complete.'
    }
}

if (-not (Test-Path -LiteralPath $RouteMarkerDir -PathType Container)) {
    New-Item -ItemType Directory -Path $RouteMarkerDir -Force | Out-Null
}
$markerTemp = "$RouteMarkerFile.tmp"
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $markerText = "route=$installedWith`npath=$needsPath`nversion=1`n"
    [IO.File]::WriteAllText($markerTemp, $markerText, $utf8)
    Move-Item -LiteralPath $markerTemp -Destination $RouteMarkerFile -Force
} catch {
    if (Test-Path -LiteralPath $markerTemp) { Remove-Item -LiteralPath $markerTemp -Force -ErrorAction SilentlyContinue }
    Write-Fail "could not record the installation route: $($_.Exception.Message)"
}

Write-Ok "Neo $neoVersion installed via $installedWith."
Write-Note "location: $neoBin"
Write-Note 'fresh PowerShell process: both neo and harness resolve to the installed Neo route'
if ($pathOnPath) {
    Write-Note 'this PowerShell process PATH: updated (intended route first)'
} else {
    Write-Warn2 'this PowerShell process PATH does not have the intended route first; the fresh process check still passed'
}
if ($oldUserFirst -and ((Normalize-PathEntry $oldUserFirst).ToLowerInvariant() -eq (Normalize-PathEntry $needsPath).ToLowerInvariant()) -and $newUserPath -eq $userPath) {
    Write-Note 'user PATH: already normalized with the intended route first'
} elseif ($routeWasPresent) {
    Write-Note "reordered $needsPath to the front of the user PATH"
} else {
    Write-Note "added $needsPath to the user PATH"
}
if ($previousRoute -and $previousRoute -ne $installedWith) {
    Write-Note "route transition: $previousRoute -> $installedWith"
}
Write-Host ''
Write-Host 'Run neo to get started.'
Write-Host "Docs: https://github.com/$NeoRepo#readme"
Write-Host ''
