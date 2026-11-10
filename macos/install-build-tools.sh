#!/usr/bin/env bash

# Installs the public build tools needed by ./build.sh on macOS: Python 3.12,
# Node.js LTS, and the Rust toolchain. Command Line Tools for Xcode (clang, the
# linker) are required by Cargo and are installed separately below.

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer must run on macOS." >&2
  exit 1
fi

# --- Xcode Command Line Tools (clang / ld) ----------------------------------
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Installing the Xcode Command Line Tools (clang, linker)..."
  # Opens a GUI installer; wait for it to finish before continuing.
  xcode-select --install || true
  echo "Finish the Command Line Tools installation, then re-run this script." >&2
  exit 1
fi

# --- Homebrew ----------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew..."
  /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Make brew available in this shell for the rest of the script.
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
fi

echo "Installing Python 3.12..."
brew install python@3.12

echo "Installing Node.js LTS..."
brew install node

echo "Installing the Rust toolchain..."
if ! command -v rustup >/dev/null 2>&1 && ! command -v rustc >/dev/null 2>&1; then
  brew install rustup
  rustup-init -y --default-toolchain stable --no-modify-path
fi
# Ensure a stable toolchain is selected regardless of how rustup was obtained.
if command -v rustup >/dev/null 2>&1; then
  rustup default stable
fi

echo ""
echo "Build tools installation completed."
echo "Open a new terminal in macos_build so PATH picks up cargo, then run:"
echo "  ./build.sh"
