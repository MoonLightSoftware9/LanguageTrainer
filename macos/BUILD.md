# macOS portable build

This file is part of the tracked `macos` source template. On Linux (or macOS), run `./prepare_macos_build.sh` from the repository root. The script refreshes sources and assets in `macos_build` while preserving build caches as a self-contained project snapshot. Copy only that generated directory to a Mac; the rest of the repository is not required.

The build must run **on a Mac** and targets the architecture of that Mac — Apple Silicon produces an `arm64` build, Intel produces an `x86_64` build.

Install the required public build tools:

```bash
chmod +x install-build-tools.sh build.sh
./install-build-tools.sh
```

This installs the Xcode Command Line Tools (clang, linker), Homebrew, Python 3.12, Node.js LTS, and the Rust toolchain. The full Xcode IDE is not required.

Open a new terminal in this directory (so `PATH` picks up `cargo`) and run:

```bash
./build.sh
```

The script creates a Python virtual environment, packages the backend with PyInstaller, builds the Tauri shell, ad-hoc code-signs every binary, and creates:

```text
dist/LanguageTrainer-macos-<arch>-0.4.2.zip
```

The ZIP is the application distribution. Unzip it without changing its internal layout and run `LanguageTrainer`. Python, Docker, Node.js, and Rust are not required on the target computer.

If both `dict-backend` and the Tauri executable were built successfully but packaging failed, continue without recompiling:

```bash
./build.sh --package-only
```

To skip dependency installation on a rebuild (venv + npm already populated):

```bash
./build.sh --skip-dependency-install
```

## Notes

- **Gatekeeper / quarantine.** The binaries are ad-hoc signed (no Apple Developer certificate), which is enough to run locally on the machine that built them. If you copy the ZIP to another Mac and it is blocked as "damaged" or "from an unidentified developer", clear the download quarantine flag once after unzipping:

  ```bash
  xattr -dr com.apple.quarantine "LanguageTrainer-macos-<arch>-0.4.2"
  ```

- **Packaging is non-destructive.** Packaging stages into `.build/package` and zips into `dist/`; it never touches an installed application's `data` directory, so a user database cannot be damaged by a rebuild.
