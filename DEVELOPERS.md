# LanguageTrainer — Developer Guide

This document covers installation, deployment, command-line administration, building, and release management for LanguageTrainer. End-user and interface documentation lives in [docs/manual/README.md](docs/manual/README.md) and [docs/manual/README.ru.md](docs/manual/README.ru.md). The root README is the English project overview.

## Running On Windows

The portable Windows distribution is the primary desktop package. Extract it to a writable directory and start `LanguageTrainer.exe`. Application data is kept separately from bundled resources, so replacing the program files does not require replacing the database.

To prepare the Windows source bundle on Linux:

```bash
./prepare_win64_build.sh
```

Then on Windows:

```powershell
cd win64_build
.\build.ps1
```

The result contains the desktop executable, backend, static English and Russian README files, local dictionaries, WordNet data, and the Argos model used for book translation.

## Running With Docker

Build the image:

```bash
docker build -t languagetrainer:0.5.0 .
```

Run it with persistent data:

```bash
mkdir -p data
docker run --rm -it \
  -p 8000:8000 \
  -v "$PWD/data:/data" \
  languagetrainer:0.5.0
```

Open `http://127.0.0.1:8000/`. The `/data` mount contains the database and settings.

## Local Python Run

Create the environment and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Start the server:

```bash
.venv/bin/python dict.py server --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

For performance diagnostics, add `--debug`. A floating panel records every API request and separates Python server processing, fetch/time-to-first-byte, response transfer, JSON parsing, and Vue rendering. The panel also shows response size and identifies the largest timing component. It is not present during a normal run.

```bash
.venv/bin/python dict.py server --host 127.0.0.1 --port 8000 --debug
```

## Command Line Administration

Initialize a database:

```bash
.venv/bin/python dict.py server --init --name Alice --host 127.0.0.1 --port 8000
```

List collections:

```bash
.venv/bin/python dict.py control --user Alice --list-collections
```

Create and load a dictionary:

```bash
.venv/bin/python dict.py control --user Alice \
  --create-collection --name "Business words" --type words --lang EN

.venv/bin/python dict.py control --user Alice --collection_id 4 \
  --load-words words.txt --lang EN
```

Update vendored Vue and the Argos book-translation model:

```bash
.venv/bin/python dict.py control --update
```

Use `--database /path/to/file.sqlite3` to administer a specific database.

## Local Data And Dependencies

The application is designed to work locally:

- SQLite stores users, collections, progress, words, reminders, meanings, and quotations.
- Vue is vendored under `vendor/`.
- English definitions use bundled WordNet data.
- Greek/Russian-oriented lookup uses `elru.db`.
- Pronunciations use bundled sound and Macmillan resources where available.
- Argos Translate is used for optional English-to-Russian book passage translation, not for README generation.
- Word lookups (the definition dropdown) are served from `dictionaries.db`, a prebuilt SQLite cache of the parsed StarDict entries. It is copied as-is into Docker and desktop builds. Large assets are distributed separately from a clean source export. Regenerate it with `python build_dictionaries_db.py` only when the underlying StarDict dictionaries change (it reads them and rewrites `dictionaries.db` next to them). When absent the app falls back to reading the StarDict files directly.
- Books can be translated offline: enabling translation on a book has a background worker fill a per-chunk translation cache ahead of the reading position, and the reading-mode translation reads that cache.

The En and Ru README pages are independent, manually maintained Markdown documents embedded into the initial application page. Switching languages does not make a network request and does not translate anything at runtime.

## Release Version Management

The application version is defined by `APP_VERSION` in `dict.py`. Update it with the project helper used by the release workflow, verify `--version`, rebuild `win64_build`, and tag the same version consistently. Do not edit generated Windows output as a source file.

## Public snapshot

This directory is a standalone source snapshot of version 0.5.0. It includes `win64_build` with all offline inputs and `prepare_win64_build.sh` to regenerate it. User databases, settings, build caches, and the original Git history are excluded.

Install Git LFS before adding files to a new repository. `.gitattributes` tracks the large dictionary, audio, and model files with LFS. Initialize Git here and make the initial commit; no previous history is included.

The supplied `data/argos-translate` and `data/nltk_data` contain only bundled offline dependencies. GitHub Actions checks out LFS assets and builds without a separate asset download URL.

## Default languages

English and Russian are enabled. Greek (`CY` / `EL`) is hidden from language choices and collection lists, and its database is omitted from desktop and Docker packages. Existing Greek collections are preserved. To enable them in a source installation, set `DICT_ENABLE_GREEK=1` and restore `elru.db` in the project root.

The Windows backend uses a copied Python 3.12 runtime with ordinary `.py` sources. The desktop window uses bundled Qt WebEngine, with software rendering and native startup/error status. A prebuilt distlib launcher starts the bundled Python through a path relative to the executable. No Python freezing, Rust, Node.js, or C++ toolchain is needed for Windows packaging. Python, the browser engine, and C++ runtime DLLs are included; neither WebView2 nor .NET is required on the target PC. Packaging audits native dependencies and verifies isolated imports. macOS uses PyInstaller for runtime packaging.
