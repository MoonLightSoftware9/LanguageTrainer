param(
    [string]$Version = "0.5.0",
    [switch]$SkipDependencyInstall,
    [switch]$PackageOnly
)

$ErrorActionPreference = "Stop"
$BundleRoot = (Resolve-Path $PSScriptRoot).Path
$BackendRoot = Join-Path $BundleRoot "backend"
$SourceResources = Join-Path $BundleRoot "resources"
$SeedData = Join-Path $BundleRoot "seed_data"
$BuildRoot = Join-Path $BundleRoot ".build"
$VenvRoot = Join-Path $BuildRoot "venv"
$Python = Join-Path $VenvRoot "Scripts/python.exe"
$BackendBuild = Join-Path $BuildRoot "backend"
$PortableName = "LanguageTrainer-windows-x64-$Version"
$DistRoot = Join-Path $BundleRoot "dist"
$PortableRoot = Join-Path $BuildRoot "package/$PortableName"
$ResourcesRoot = Join-Path $PortableRoot "resources"
$DataRoot = Join-Path $PortableRoot "data"

if ($env:OS -ne "Windows_NT") {
    throw "The portable Windows build must run on Windows."
}

New-Item -ItemType Directory -Force $BuildRoot | Out-Null
$BuildTimer = [System.Diagnostics.Stopwatch]::StartNew()
$script:CurrentStage = "Starting"
$script:StageTimer = [System.Diagnostics.Stopwatch]::StartNew()
$BuildLog = Join-Path $BuildRoot "build.log"
$TranscriptStarted = $false
try {
    Start-Transcript -Path $BuildLog -Append -ErrorAction Stop | Out-Null
    $TranscriptStarted = $true
}
catch { Write-Warning "Could not start build log: $_" }

function Set-BuildStage([string]$Name) {
    Write-Host ("[done] {0}: {1:N1}s" -f $script:CurrentStage, $script:StageTimer.Elapsed.TotalSeconds)
    $script:CurrentStage = $Name
    $script:StageTimer.Restart()
    Write-Host ("[{0:HH:mm:ss}] {1} (total {2:N0}s)" -f (Get-Date), $Name, $BuildTimer.Elapsed.TotalSeconds) -ForegroundColor Cyan
}

try {
Write-Host "Build log: $BuildLog"
if (-not $PackageOnly) {
$MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$MachinePath;$UserPath;$env:Path"

Set-BuildStage "Checking Python environment"

function Find-Python312 {
    $Candidates = @(
        @{ Command = "py"; Arguments = @("-3.12") },
        @{ Command = "python"; Arguments = @() },
        @{ Command = "python3"; Arguments = @() },
        @{ Command = (Join-Path $env:LocalAppData "Programs/Python/Python312/python.exe"); Arguments = @() },
        @{ Command = (Join-Path $env:ProgramFiles "Python312/python.exe"); Arguments = @() },
        @{ Command = "C:/Python312/python.exe"; Arguments = @() }
    )

    foreach ($Candidate in $Candidates) {
        $CommandInfo = Get-Command $Candidate.Command -ErrorAction SilentlyContinue
        if (-not $CommandInfo) { continue }

        try {
            $Executable = $CommandInfo.Source
            $Arguments = $Candidate.Arguments
            $DetectedVersion = & $Executable @Arguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $DetectedVersion -eq "3.12") {
                return [PSCustomObject]@{
                    Executable = $Executable
                    Arguments = $Arguments
                }
            }
        }
        catch {
            continue
        }
    }
    return $null
}

$BootstrapPython = Find-Python312
if (-not $BootstrapPython) {
    throw @"
Python 3.12 was not found. Install it, reopen PowerShell, and run build.ps1 again:

./install-build-tools.ps1
"@
}

New-Item -ItemType Directory -Force $BuildRoot, $BackendBuild | Out-Null

if (-not (Test-Path $Python)) {
    $BootstrapExecutable = $BootstrapPython.Executable
    $BootstrapArguments = $BootstrapPython.Arguments
    & $BootstrapExecutable @BootstrapArguments -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python virtual environment." }
}

if (-not $SkipDependencyInstall) {
    Set-BuildStage "Installing Python dependencies"
    # Install everything except argostranslate, then install argostranslate with
    # --no-deps so its stanza(->torch) requirement is skipped. torch (~1 GB, plus
    # CUDA on some platforms) is never used at runtime: sentence segmentation goes
    # through spaCy (ARGOS_CHUNK_TYPE=SPACY). A stub 'stanza' is dropped into
    # site-packages to satisfy argostranslate's unguarded `import stanza`.
    $Requirements = Join-Path $BackendRoot "requirements.txt"
    $FilteredRequirements = Join-Path $BuildRoot "requirements-nostanza.txt"
    Get-Content $Requirements |
        Where-Object { $_ -notmatch '^\s*argostranslate\s*$' } |
        Set-Content $FilteredRequirements
    & $Python -m pip install -r $FilteredRequirements
    if ($LASTEXITCODE -ne 0) { throw "Failed to install backend dependencies." }
    & $Python -m pip install --no-deps argostranslate
    if ($LASTEXITCODE -ne 0) { throw "Failed to install argostranslate." }

    # Replace any real stanza with the torch-free stub.
    $SitePackages = & $Python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    if ($LASTEXITCODE -ne 0) { throw "Failed to resolve site-packages path." }
    $StanzaTarget = Join-Path $SitePackages "stanza"
    if (Test-Path $StanzaTarget) { Remove-Item -Recurse -Force $StanzaTarget }
    Copy-Item -Recurse (Join-Path $BackendRoot "stubs/stanza") $StanzaTarget
}

Set-BuildStage "Checking desktop dependencies"

$DesktopRequirements = Join-Path $BundleRoot "requirements-desktop.txt"
$DesktopHash = (Get-FileHash $DesktopRequirements -Algorithm SHA256).Hash
$DesktopStamp = Join-Path $BuildRoot "desktop-requirements.sha256"
if (-not (Test-Path $DesktopStamp) -or (Get-Content $DesktopStamp -Raw).Trim() -ne $DesktopHash) {
    & $Python -m pip install --only-binary=:all: -r $DesktopRequirements
    if ($LASTEXITCODE -ne 0) { throw "Failed to install bundled browser dependencies." }
    Set-Content -Path $DesktopStamp -Value $DesktopHash
}

Set-BuildStage "Staging Python runtime and sources"

# Copy the existing Python runtime and readable sources. No freezing or C build.
& $Python -u (Join-Path $BundleRoot "package_backend.py") $BackendRoot (Join-Path $BackendBuild "dict-backend") | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Backend staging failed." }
Set-BuildStage "Checking bundled DLL and browser dependencies"
& $Python -u (Join-Path $BundleRoot "runtime_audit.py") (Join-Path $BackendBuild "dict-backend") | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Portable dependency audit failed." }
Set-BuildStage "Testing isolated bundled Python imports"
& (Join-Path $BackendBuild "dict-backend/python.exe") -I -c "import sys; from pathlib import Path; root=Path(sys.executable).resolve().parent; assert sys.flags.isolated and Path(sys.prefix).resolve()==root; import sqlmodel, spacy, ctranslate2, PySide6.QtWebEngineWidgets; assert all(Path(m.__file__).resolve().is_relative_to(root) for m in (sqlmodel, spacy, ctranslate2, PySide6)); print('Isolated portable imports OK')" | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Portable Python import check failed." }

}

Set-BuildStage "Updating portable directory (reuse unchanged files)"
New-Item -ItemType Directory -Force $DistRoot, $ResourcesRoot, $DataRoot | Out-Null

$DesktopExe = Join-Path $BackendBuild "LanguageTrainer.exe"
$BackendDist = Join-Path $BackendBuild "dict-backend"
if (-not (Test-Path $DesktopExe)) { throw "Desktop launcher not found: $DesktopExe" }
if (-not (Test-Path (Join-Path $BackendDist "python.exe"))) { throw "Backend standalone dist folder not found under: $BackendBuild" }

$PackagingPython = $Python
if (-not (Test-Path $PackagingPython)) { $PackagingPython = Join-Path $BackendDist "python.exe" }
$ProgressHelper = Join-Path $BundleRoot "package_progress.py"
function Copy-PackageTree([string]$Source, [string]$Destination, [switch]$Runtime) {
    $CopyArguments = @("-u", $ProgressHelper, "copy", $Source, $Destination)
    if ($Runtime) { $CopyArguments += "--runtime" }
    & $PackagingPython @CopyArguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Copy failed: $Source -> $Destination" }
}

Set-BuildStage "Reusing prepared Python runtime"
# Refresh only application code; PackageOnly reuses the already prepared runtime.
if ($PackageOnly) {
    Set-BuildStage "Refreshing application sources only"
    foreach ($Name in @("dict.py", "get_sounds.py", "lookup_parse.py", "translate.py")) {
        Copy-Item -Force (Join-Path $BackendRoot $Name) (Join-Path $BackendDist $Name)
    }
    Copy-Item -Force (Join-Path $BundleRoot "desktop.py") (Join-Path $BackendDist "desktop.py")
    Copy-PackageTree (Join-Path $BackendRoot "dictapp") (Join-Path $BackendDist "dictapp") -Runtime
}
Copy-Item $DesktopExe (Join-Path $PortableRoot "LanguageTrainer.exe")
$PortableBackend = Join-Path $PortableRoot "backend"
if ((Test-Path $PortableBackend) -and -not ((Get-Item $PortableBackend -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    # Renaming a previous partial copy on the same volume avoids deleting or
    # recopying thousands of files. Keep it outside the release archive.
    $PreviousBackend = Join-Path $BuildRoot ("previous-staging-backend-" + [Guid]::NewGuid().ToString("N"))
    Move-Item -LiteralPath $PortableBackend -Destination $PreviousBackend
    Write-Host "Previous partial staging copy retained at $PreviousBackend"
}
if (-not (Test-Path $PortableBackend)) {
    try {
        New-Item -ItemType Junction -Path $PortableBackend -Target $BackendDist -ErrorAction Stop | Out-Null
        Write-Host "Reusing prepared runtime through a staging junction; no second runtime copy." -ForegroundColor Green
    }
    catch {
        Write-Warning "Staging junction unavailable; using incremental native copy."
        Copy-PackageTree $BackendDist $PortableBackend -Runtime
    }
}
elseif ((Get-Item $PortableBackend -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    Write-Host "Reusing staged runtime junction." -ForegroundColor Green
}
else {
    Copy-PackageTree $BackendDist $PortableBackend -Runtime
}

Set-BuildStage "Copying dictionaries, audio, books and interface"
$RequiredResources = @(
    "SoundEn.lsa", "1_star.txt", "2_star.txt", "3_star.txt",
    "zen.txt", "thinking-fast-and-slow.txt", "docs/manual/README.md", "docs/manual/README.ru.md", "web", "vendor", "dictionaries.db"
)
foreach ($Item in $RequiredResources) {
    if (-not (Test-Path (Join-Path $SourceResources $Item))) {
        throw "Required runtime resource not found: resources/$Item"
    }
}
Copy-PackageTree $SourceResources $ResourcesRoot

Set-BuildStage "Copying offline translation model"
$ArgosSeed = Join-Path $SeedData "argos-translate"
$ArgosTarget = Join-Path $DataRoot "argos-translate"
if (-not (Test-Path (Join-Path $ArgosSeed "packages"))) {
    throw "Bundled Argos model not found: seed_data/argos-translate/packages"
}
New-Item -ItemType Directory -Force $ArgosTarget | Out-Null
Copy-PackageTree $ArgosSeed $ArgosTarget

Set-BuildStage "Copying WordNet"
$NltkSeed = Join-Path $SeedData "nltk_data"
if (-not (Test-Path (Join-Path $NltkSeed "corpora/wordnet.zip"))) {
    throw "Bundled WordNet data not found: seed_data/nltk_data/corpora/wordnet.zip"
}
Copy-PackageTree $NltkSeed (Join-Path $ResourcesRoot "nltk_data")

Set-BuildStage "Testing bundled browser, backend and interface"
$SmokeData = Join-Path $BuildRoot ("startup-check-" + [Guid]::NewGuid().ToString("N"))
$PreviousRoot = $env:DICT_PORTABLE_ROOT
$PreviousData = $env:DICT_DESKTOP_DATA_DIR
$PreviousPlatform = $env:QT_QPA_PLATFORM
try {
    $env:DICT_PORTABLE_ROOT = $PortableRoot
    $env:DICT_DESKTOP_DATA_DIR = $SmokeData
    $env:QT_QPA_PLATFORM = "offscreen"
    $SmokeProcess = Start-Process -FilePath (Join-Path $PortableRoot "LanguageTrainer.exe") -ArgumentList @("--smoke-test") -PassThru -NoNewWindow
    $null = $SmokeProcess.Handle
    if (-not $SmokeProcess.WaitForExit(180000)) {
        & taskkill /PID $SmokeProcess.Id /T /F | Out-Host
        throw "Desktop startup test timed out. Log: $SmokeData/desktop.log"
    }
    if ($SmokeProcess.ExitCode -ne 0) {
        if (Test-Path (Join-Path $SmokeData "desktop.log")) { Get-Content (Join-Path $SmokeData "desktop.log") -Tail 40 | Out-Host }
        throw "Desktop startup test failed. Log: $SmokeData/desktop.log"
    }
    if (-not (Select-String -Path (Join-Path $SmokeData "desktop.log") -SimpleMatch "Application ready" -Quiet)) { throw "Launcher exited without confirming interface readiness. Log: $SmokeData/desktop.log" }
    Write-Host "Bundled browser, backend health check and interface startup OK" -ForegroundColor Green
}
finally {
    $env:DICT_PORTABLE_ROOT = $PreviousRoot
    $env:DICT_DESKTOP_DATA_DIR = $PreviousData
    $env:QT_QPA_PLATFORM = $PreviousPlatform
}

$Archive = Join-Path $DistRoot "$PortableName.zip"
if (Test-Path $Archive) { Remove-Item -Force $Archive }
Set-BuildStage "Creating ZIP archive (fast compression)"
& $PackagingPython -u $ProgressHelper zip $PortableRoot $Archive | Out-Host
if ($LASTEXITCODE -ne 0) { throw "ZIP creation failed." }
if (-not (Test-Path $Archive)) { throw "Archive was not created: $Archive" }

Write-Host "Portable staging directory: $PortableRoot"
Write-Host "Archive: $Archive"

Set-BuildStage "Complete"
}
catch {
    Write-Host ("[FAILED] {0} after {1:N1}s: {2}" -f $script:CurrentStage, $script:StageTimer.Elapsed.TotalSeconds, $_) -ForegroundColor Red
    Write-Host "Details: $BuildLog"
    throw
}
finally {
    Write-Host ("Total elapsed: {0:N1}s" -f $BuildTimer.Elapsed.TotalSeconds)
    if ($TranscriptStarted) { Stop-Transcript | Out-Null }
}
