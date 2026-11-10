# Windows x64 portable package

On Linux, run `./prepare_win64_build.sh` and copy `win64_build` to Windows.

Python 3.12 is the only packaging tool. The application uses Microsoft Edge WebView2 Runtime for its window. To install or update both:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./install-build-tools.ps1
```

Open a new PowerShell in the same directory and package the application:

```powershell
./build.ps1
```

The script installs Python dependencies, copies the runtime and sources, adds a prebuilt GUI launcher, and creates `dist/LanguageTrainer-windows-x64-0.4.2.zip`. No Rust, Node.js, Visual Studio, or source compilation is involved.

Extract the ZIP and open `LanguageTrainer.exe`. The target PC needs WebView2 Runtime; it does not need Python installed. Progress, settings, and translation models stay in `data`.

For a subsequent package with existing dependencies:

```powershell
./build.ps1 -SkipDependencyInstall
```

To repeat only final assembly and ZIP creation:

```powershell
./build.ps1 -PackageOnly
```

Staging uses `.build/package/LanguageTrainer-windows-x64-0.4.2`. Existing application data is preserved. ZIP uses fast compression. First-time downloads still depend on network speed.

## Sync code updates from WSL

After preparing the bundle, sync small files while keeping installed dependencies, packaged applications, dictionaries, audio, and translation models on Windows:

```bash
rsync -av \
  --exclude='/.build/' --exclude='/dist/' --exclude='/seed_data/' \
  --exclude='/resources/*.db' --exclude='/resources/*.lsa' \
  --exclude='__pycache__/' \
  win64_build/ /mnt/c/Users/ebazhin/Documents/win64_build/
```

This updates the source bundle. A packaged application has its own `backend` directory; an applicable source-only hotfix can be applied there while the application is closed, without rebuilding.

## Packaging diagnostics

Each stage prints its start time and elapsed seconds. Copying and ZIP creation report bytes, file counts, percent complete, MiB/s, and the current file about every two seconds. The full console transcript is appended to `.build/build.log`; failures name the stage that stopped.

To watch the log from another PowerShell window:

```powershell
Get-Content .build/build.log -Tail 30 -Wait
```
