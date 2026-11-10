$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "This installer must run on Windows."
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is not available. Install or update 'App Installer' from Microsoft Store."
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    Write-Host "Installing $Name..." -ForegroundColor Cyan
    & winget install @Arguments `
        --source winget `
        --accept-package-agreements `
        --accept-source-agreements `
        --disable-interactivity

    $InstallExitCode = $LASTEXITCODE
    switch ($InstallExitCode) {
        0 { return }
        3010 {
            Write-Host "$Name installed; a restart may be required." -ForegroundColor Yellow
            return
        }
        -1978335189 {
            # APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE (0x8A15002B).
            Write-Host "$Name is already installed; no update is available." -ForegroundColor Green
            return
        }
        default { throw "$Name installation failed with exit code $InstallExitCode." }
    }
}

Install-WingetPackage -Name "Python 3.12" -Arguments @(
    "--exact", "--id", "Python.Python.3.12",
    "--override", "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=0"
)

Install-WingetPackage -Name "Microsoft Edge WebView2 Runtime" -Arguments @(
    "--exact", "--id", "Microsoft.EdgeWebView2Runtime"
)

Write-Host "Build tools installation completed." -ForegroundColor Green
Write-Host "Open a new PowerShell window in win64_build, then run ./build.ps1"
