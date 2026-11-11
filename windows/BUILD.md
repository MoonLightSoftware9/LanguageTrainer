# Windows x64 portable package

On Linux, run `./prepare_win64_build.sh` and copy `win64_build` to Windows.

Python 3.12 is the only packaging tool. The package includes Python, Qt WebEngine (Chromium), and the Microsoft C++ runtime DLLs. To install or update Python:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./install-build-tools.ps1
```

Open a new PowerShell in the same directory and package the application:

```powershell
./build.ps1
```

The script installs Python dependencies, copies the runtime and sources, adds a prebuilt GUI launcher, and creates `dist/LanguageTrainer-windows-x64-0.5.0.zip`. No Rust, Node.js, Visual Studio, or source compilation is involved.

Extract the ZIP and open `LanguageTrainer.exe`. The target PC needs Windows 10/11 x64. It does not need Python, WebView2, .NET, or Visual C++ Redistributable installed. Progress, settings, and translation models stay in `data`.

For a subsequent package with existing dependencies:

```powershell
./build.ps1 -SkipDependencyInstall
```

To repeat only final assembly and ZIP creation:

```powershell
./build.ps1 -PackageOnly
```

Staging uses `.build/package/LanguageTrainer-windows-x64-0.5.0`. Existing application data is preserved. ZIP uses fast compression. First-time downloads still depend on network speed. Qt includes the browser engine, so the archive is larger than the earlier system-WebView2 package.

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

## Startup checks

The launcher shows native startup status until the backend health check and Vue interface are ready. Failures and timeouts show an error page with the last backend output and an Open log folder button. `data/desktop.log` includes Qt and JavaScript messages. Software browser rendering is enabled to avoid GPU-driver blank screens.

Packaging checks PE imports for unbundled non-Windows DLLs, verifies Qt browser resources, imports critical modules using the isolated bundled interpreter, and starts the bundled browser/backend/interface offscreen before ZIP creation. A package whose startup check fails is not archived. `python312._pth` prevents fallback to an installed Python or user site packages. The EXE launcher is tested from a fresh interpreter without packaging modules available.

Runtime staging uses Windows robocopy with 16 workers and reuses unchanged files. Python tests, IDLE, QML, Qt SDK headers and type stubs are excluded before traversal. The final staging directory links to the prepared runtime through an NTFS junction; ZIP creation reads its files directly instead of copying the runtime a second time. ZIP entries contain actual files, not junctions. On filesystems without junction support, staging falls back to incremental robocopy.

`-PackageOnly` refreshes application source files and assembles the existing runtime without dependency installation or repeating the runtime copy.
