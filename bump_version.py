#!/usr/bin/env python3

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def replace_exact(path, old, new, expected_count):
    text = path.read_text(encoding="utf-8")
    actual_count = text.count(old)
    if expected_count is None and actual_count < 1:
        raise SystemExit(
            f"Refusing to update {path.relative_to(ROOT)}: {old!r} was not found."
        )
    if expected_count is not None and actual_count != expected_count:
        raise SystemExit(
            f"Refusing to update {path.relative_to(ROOT)}: expected "
            f"{expected_count} occurrence(s) of {old!r}, found {actual_count}."
        )
    path.write_text(text.replace(old, new), encoding="utf-8")


def current_version():
    text = (ROOT / "dictapp" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"$', text, re.MULTILINE)
    if not match:
        raise SystemExit("Cannot find APP_VERSION in dictapp/config.py.")
    return match.group(1)


def source_replacements(old, new):
    return (
        ("dictapp/config.py", f'APP_VERSION = "{old}"', f'APP_VERSION = "{new}"', 1),
        ("Dockerfile", f"ARG APP_VERSION={old}", f"ARG APP_VERSION={new}", 1),
        ("README.md", old, new, None),
        ("docs/manual/README.md", old, new, None),
        ("docs/manual/README.ru.md", old, new, None),
        ("DEVELOPERS.md", old, new, None),
        ("DEVELOPERS.ru.md", old, new, None),
        ("docker_hub_info.md", old, new, None),
        (".github/workflows/windows-portable.yml", old, new, 4),
        ("windows/build.ps1", f'[string]$Version = "{old}"', f'[string]$Version = "{new}"', 1),
        ("windows/BUILD.md", old, new, 2),
        ("macos/build.sh", f'VERSION="{old}"', f'VERSION="{new}"', 1),
        ("macos/BUILD.md", old, new, 2),
        ("macos/tauri/package.json", f'  "version": "{old}",', f'  "version": "{new}",', 1),
        (
            "macos/tauri/package-lock.json",
            f'  "name": "dict-typing-trainer-desktop",\n  "version": "{old}",',
            f'  "name": "dict-typing-trainer-desktop",\n  "version": "{new}",',
            1,
        ),
        (
            "macos/tauri/package-lock.json",
            f'      "name": "dict-typing-trainer-desktop",\n      "version": "{old}",',
            f'      "name": "dict-typing-trainer-desktop",\n      "version": "{new}",',
            1,
        ),
        ("macos/tauri/src-tauri/Cargo.toml", f'version = "{old}"', f'version = "{new}"', 1),
        (
            "macos/tauri/src-tauri/Cargo.lock",
            f'name = "dict-typing-trainer"\nversion = "{old}"',
            f'name = "dict-typing-trainer"\nversion = "{new}"',
            1,
        ),
        ("macos/tauri/src-tauri/tauri.conf.json", f'  "version": "{old}",', f'  "version": "{new}",', 1),
    )


def main():
    parser = argparse.ArgumentParser(
        description="Update the application version and refresh win64_build."
    )
    parser.add_argument("version", help="new semantic version, for example 0.2.12")
    args = parser.parse_args()

    if not SEMVER.fullmatch(args.version):
        parser.error("version must use semantic versioning, for example 0.2.12")

    old_version = current_version()
    if args.version == old_version:
        raise SystemExit(f"Application version is already {args.version}.")

    replacements = source_replacements(old_version, args.version)
    for relative_path, old, _new, expected_count in replacements:
        path = ROOT / relative_path
        if not path.is_file():
            raise SystemExit(f"Required versioned file is missing: {relative_path}")
        actual_count = path.read_text(encoding="utf-8").count(old)
        if expected_count is None and actual_count < 1:
            raise SystemExit(
                f"Version consistency check failed for {relative_path}: {old!r} was not found."
            )
        if expected_count is not None and actual_count != expected_count:
            raise SystemExit(
                f"Version consistency check failed for {relative_path}: expected "
                f"{expected_count} occurrence(s) of {old!r}, found {actual_count}."
            )

    for relative_path, old, new, expected_count in replacements:
        replace_exact(ROOT / relative_path, old, new, expected_count)

    for build_dir, generator_name in (
        ("win64_build", "prepare_win64_build.sh"),
        ("macos_build", "prepare_macos_build.sh"),
    ):
        if (ROOT / build_dir).exists():
            subprocess.run([str(ROOT / generator_name)], cwd=ROOT, check=True)

    print(f"Application version updated: {old_version} -> {args.version}")


if __name__ == "__main__":
    main()
