#!/usr/bin/env bash

# Portable macOS build. Mirrors windows/build.ps1: packages the Python backend
# with PyInstaller, builds the Tauri shell, and stages a self-contained
# portable folder (LanguageTrainer + backend/ + resources/ + data/) which is
# then zipped into dist/. Python, Node, and Rust are only needed on the build
# machine, not on the target.

set -euo pipefail

VERSION="0.5.0"
SKIP_DEPENDENCY_INSTALL=0
PACKAGE_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --skip-dependency-install) SKIP_DEPENDENCY_INSTALL=1; shift ;;
    --package-only) PACKAGE_ONLY=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "The portable macOS build must run on macOS." >&2
  exit 1
fi

BUNDLE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BACKEND_ROOT="$BUNDLE_ROOT/backend"
TAURI_ROOT="$BUNDLE_ROOT/tauri"
SOURCE_RESOURCES="$BUNDLE_ROOT/resources"
SEED_DATA="$BUNDLE_ROOT/seed_data"
BUILD_ROOT="$BUNDLE_ROOT/.build"
VENV_ROOT="$BUILD_ROOT/venv"
PYTHON="$VENV_ROOT/bin/python"
BACKEND_BUILD="$BUILD_ROOT/backend"
ARCH=$(uname -m)   # arm64 (Apple Silicon) or x86_64 (Intel)
PORTABLE_NAME="LanguageTrainer-macos-$ARCH-$VERSION"
DIST_ROOT="$BUNDLE_ROOT/dist"
PORTABLE_ROOT="$BUILD_ROOT/package/$PORTABLE_NAME"
RESOURCES_ROOT="$PORTABLE_ROOT/resources"
DATA_ROOT="$PORTABLE_ROOT/data"

find_python312() {
  local candidate detected
  for candidate in \
    python3.12 \
    /opt/homebrew/opt/python@3.12/bin/python3.12 \
    /usr/local/opt/python@3.12/bin/python3.12 \
    python3; do
    if command -v "$candidate" >/dev/null 2>&1 || [[ -x "$candidate" ]]; then
      detected=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
      if [[ "$detected" == "3.12" ]]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

if [[ "$PACKAGE_ONLY" -eq 0 ]]; then
  for command in npm cargo; do
    if ! command -v "$command" >/dev/null 2>&1; then
      echo "Required build command is not available: $command. Run ./install-build-tools.sh first." >&2
      exit 1
    fi
  done

  if ! BOOTSTRAP_PYTHON=$(find_python312); then
    echo "Python 3.12 was not found. Install it and retry:" >&2
    echo "  ./install-build-tools.sh" >&2
    exit 1
  fi

  mkdir -p "$BUILD_ROOT" "$BACKEND_BUILD"

  if [[ ! -x "$PYTHON" ]]; then
    "$BOOTSTRAP_PYTHON" -m venv "$VENV_ROOT"
  fi

  if [[ "$SKIP_DEPENDENCY_INSTALL" -eq 0 ]]; then
    "$PYTHON" -m pip install --upgrade pip

    # Install everything except argostranslate, then install argostranslate with
    # --no-deps so its stanza(->torch) requirement is skipped. torch (~1 GB) is
    # never used at runtime: sentence segmentation goes through spaCy
    # (ARGOS_CHUNK_TYPE=SPACY). A stub 'stanza' is dropped into site-packages to
    # satisfy argostranslate's unguarded `import stanza`.
    FILTERED_REQUIREMENTS="$BUILD_ROOT/requirements-nostanza.txt"
    grep -vE '^\s*argostranslate\s*$' "$BACKEND_ROOT/requirements.txt" > "$FILTERED_REQUIREMENTS"
    "$PYTHON" -m pip install -r "$FILTERED_REQUIREMENTS" "pyinstaller>=6,<7"
    "$PYTHON" -m pip install --no-deps argostranslate

    # Replace any real stanza with the torch-free stub.
    SITE_PACKAGES=$("$PYTHON" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    STANZA_TARGET="$SITE_PACKAGES/stanza"
    rm -rf -- "$STANZA_TARGET"
    cp -a -- "$BACKEND_ROOT/stubs/stanza" "$STANZA_TARGET"
  fi

  "$PYTHON" -m PyInstaller --noconfirm --onedir --noupx \
    --name dict-backend \
    --distpath "$BACKEND_BUILD" \
    --workpath "$BUILD_ROOT/pyinstaller" \
    --specpath "$BUILD_ROOT" \
    --paths "$BACKEND_ROOT" \
    --collect-all argostranslate \
    --collect-all ctranslate2 \
    --collect-all sentencepiece \
    --collect-all sacremoses \
    --collect-all minisbd \
    --collect-all spacy \
    --collect-all nltk \
    --exclude-module torch \
    "$BACKEND_ROOT/dict.py"

  (
    cd "$TAURI_ROOT"
    if [[ "$SKIP_DEPENDENCY_INSTALL" -eq 0 ]]; then
      npm ci
    fi
    npm run build
  )
fi

# --- package ----------------------------------------------------------------

rm -rf -- "$PORTABLE_ROOT"
mkdir -p "$DIST_ROOT" "$RESOURCES_ROOT" "$DATA_ROOT"

DESKTOP_BINARY="$TAURI_ROOT/src-tauri/target/release/dict-typing-trainer"
BACKEND_DIST="$BACKEND_BUILD/dict-backend"

if [[ ! -f "$DESKTOP_BINARY" ]]; then
  echo "Tauri executable not found: $DESKTOP_BINARY" >&2
  exit 1
fi
if [[ ! -f "$BACKEND_DIST/dict-backend" ]]; then
  echo "Backend standalone dist folder not found under: $BACKEND_BUILD" >&2
  exit 1
fi

cp -- "$DESKTOP_BINARY" "$PORTABLE_ROOT/LanguageTrainer"
chmod +x "$PORTABLE_ROOT/LanguageTrainer"
cp -a -- "$BACKEND_DIST" "$PORTABLE_ROOT/backend"

REQUIRED_RESOURCES=(
  SoundEn.lsa 1_star.txt 2_star.txt 3_star.txt
  zen.txt thinking-fast-and-slow.txt docs/manual/README.md docs/manual/README.ru.md web vendor dictionaries.db
)
for item in "${REQUIRED_RESOURCES[@]}"; do
  if [[ ! -e "$SOURCE_RESOURCES/$item" ]]; then
    echo "Required runtime resource not found: resources/$item" >&2
    exit 1
  fi
done
cp -a -- "$SOURCE_RESOURCES"/* "$RESOURCES_ROOT/"

ARGOS_SEED="$SEED_DATA/argos-translate"
if [[ ! -d "$ARGOS_SEED/packages" ]]; then
  echo "Bundled Argos model not found: seed_data/argos-translate/packages" >&2
  exit 1
fi
mkdir -p "$DATA_ROOT/argos-translate"
cp -a -- "$ARGOS_SEED"/* "$DATA_ROOT/argos-translate/"

NLTK_SEED="$SEED_DATA/nltk_data"
if [[ ! -f "$NLTK_SEED/corpora/wordnet.zip" ]]; then
  echo "Bundled WordNet data not found: seed_data/nltk_data/corpora/wordnet.zip" >&2
  exit 1
fi
cp -a -- "$NLTK_SEED" "$RESOURCES_ROOT/nltk_data"

# Ad-hoc codesign every Mach-O so the loader accepts them. Apple Silicon
# refuses to run unsigned/invalidly-signed code (SIGKILL) even for local builds;
# an ad-hoc signature ("-") needs no certificate. Sign inner dylibs before the
# executables that link them.
if command -v codesign >/dev/null 2>&1; then
  find "$PORTABLE_ROOT" -type f \( -name '*.so' -o -name '*.dylib' \) \
    -exec codesign --force --timestamp=none -s - {} + 2>/dev/null || true
  codesign --force --timestamp=none -s - "$PORTABLE_ROOT/backend/dict-backend" || true
  codesign --force --timestamp=none -s - "$PORTABLE_ROOT/LanguageTrainer" || true
fi

ARCHIVE="$DIST_ROOT/$PORTABLE_NAME.zip"
rm -f -- "$ARCHIVE"
# ditto is the macOS-native archiver: it preserves symlinks, permissions, and
# resource forks/xattrs (including the ad-hoc code signatures) that a plain
# `zip` would corrupt. --keepParent nests everything under the portable folder.
ditto -c -k --sequesterRsrc --keepParent "$PORTABLE_ROOT" "$ARCHIVE"
if [[ ! -f "$ARCHIVE" ]]; then
  echo "Archive was not created: $ARCHIVE" >&2
  exit 1
fi

echo "Portable staging directory: $PORTABLE_ROOT"
echo "Archive: $ARCHIVE"
