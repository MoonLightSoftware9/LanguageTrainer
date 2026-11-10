#!/usr/bin/env python3
"""Export current public sources without rewriting the working repository."""
import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEPARATE_ASSETS = {"SoundEn.lsa", "dictionaries.db", "elru.db", "el_list.txt"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new or empty output directory")
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination == ROOT or ROOT in destination.parents:
        parser.error("Choose a destination outside the working repository")
    if destination.exists() and any(destination.iterdir()):
        parser.error("Destination must be empty")
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
    ).decode().split("\0")
    destination.mkdir(parents=True, exist_ok=True)
    for name in sorted(set(names) - SEPARATE_ASSETS - {""}):
        source = ROOT / name
        if not source.is_file():
            continue
        if source.stat().st_size >= 100 * 1024 * 1024:
            raise SystemExit(f"Move this large file to release assets first: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"Sources: {destination}")
    print("Separate release assets: " + ", ".join(sorted(SEPARATE_ASSETS)))
    print("Original files and Git history were preserved.")


if __name__ == "__main__":
    main()
