"""Copy and ZIP package files with byte progress and timings (standard library only)."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

CHUNK_SIZE = 4 * 1024 * 1024
REPORT_INTERVAL = 2.0


RUNTIME_DIRS = {"__pycache__", "test", "tests", "idlelib", "turtledemo", "ensurepip"}
QT_UNUSED_DIRS = {"qml", "include", "typesystems", "metatypes", "glue", "doc", "examples"}
QT_UNUSED_PLUGINS = {"sqldrivers", "designer", "qmltooling"}


def runtime_excluded(relative):
    parts = Path(relative).parts
    if any(part in RUNTIME_DIRS for part in parts):
        return True
    if 'PySide6' in parts:
        qt = parts[parts.index('PySide6') + 1:]
        if qt and qt[0] in QT_UNUSED_DIRS:
            return True
        if len(qt) > 1 and qt[0] == 'plugins' and qt[1] in QT_UNUSED_PLUGINS:
            return True
    return str(relative).endswith(('.pyi', '.debug.pak'))


def collect_files(source, ignore=(), runtime=False):
    print(f"Scanning {source} ...", flush=True)
    result = []
    last_report = time.monotonic()
    for folder, directories, names in os.walk(source):
        directories[:] = sorted(name for name in directories if name not in ignore and not (runtime and runtime_excluded((Path(folder) / name).relative_to(source))))
        for name in sorted(names):
            if name not in ignore and not (runtime and runtime_excluded((Path(folder) / name).relative_to(source))):
                path = Path(folder) / name
                result.append((path, path.stat().st_size))
        if time.monotonic() - last_report >= REPORT_INTERVAL:
            print(f"Scanning: {len(result):,} files found ...", flush=True)
            last_report = time.monotonic()
    return result


class Progress:
    def __init__(self, label, files):
        self.label = label
        self.total_files = len(files)
        self.total_bytes = sum(size for _, size in files)
        self.done_files = self.done_bytes = 0
        self.started = self.last_report = time.monotonic()
        self.report('starting', force=True)

    def report(self, current, force=False):
        now = time.monotonic()
        if force or now - self.last_report >= REPORT_INTERVAL:
            percent = 100 * self.done_bytes / self.total_bytes if self.total_bytes else 100
            elapsed = now - self.started
            speed = self.done_bytes / max(elapsed, 0.001) / 1024**2
            print(f"[{self.label}] {percent:5.1f}% | {self.done_files:,}/{self.total_files:,} files | "
                  f"{self.done_bytes / 1024**2:.1f}/{self.total_bytes / 1024**2:.1f} MiB | "
                  f"{speed:.1f} MiB/s | {elapsed:.0f}s | {current}", flush=True)
            self.last_report = now

    def transfer(self, source, destination, name):
        while chunk := source.read(CHUNK_SIZE):
            destination.write(chunk)
            self.done_bytes += len(chunk)
            self.report(name)
        self.done_files += 1
        self.report(name)


def copy_tree(source, destination, ignore=(), runtime=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    if source == destination or source in destination.parents:
        raise ValueError('Copy destination must be outside the source directory')
    files = collect_files(source, ignore, runtime=runtime)
    if os.name == 'nt':
        return robocopy_tree(source, destination, files, ignore, runtime)
    progress = Progress('COPY ' + source.name, files)
    destination.mkdir(parents=True, exist_ok=True)
    for path, size in files:
        name = path.relative_to(source)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == size and target.stat().st_mtime_ns == path.stat().st_mtime_ns:
            progress.done_files += 1
            progress.done_bytes += size
            progress.report(str(name) + ' (reused)')
            continue
        progress.report(str(name), force=size >= 64 * 1024**2)
        with path.open('rb') as reader, target.open('wb') as writer:
            progress.transfer(reader, writer, str(name))
        shutil.copystat(path, target)
    progress.report('complete', force=True)


def robocopy_tree(source, destination, files, ignore, runtime):
    excluded = set(ignore)
    paths = []
    if runtime:
        excluded.update(RUNTIME_DIRS)
        for prefix in (source, source / 'Lib/site-packages'):
            paths.extend(str(prefix / 'PySide6' / name) for name in QT_UNUSED_DIRS)
            paths.extend(str(prefix / 'PySide6/plugins' / name) for name in QT_UNUSED_PLUGINS)
    command = ['robocopy', str(source), str(destination), '/E', '/MT:16', '/R:1', '/W:1', '/COPY:DAT', '/DCOPY:T', '/XJ', '/NJH', '/NFL', '/NDL', '/NP']
    if excluded or paths:
        command += ['/XD', *sorted(excluded), *paths]
    excluded_files = [name for name in ignore if '.' in name]
    if runtime:
        excluded_files += ['*.pyi', '*.debug.pak']
    if excluded_files:
        command += ['/XF', *excluded_files]
    print(f'[COPY {source.name}] Robocopy: {len(files):,} selected files, 16 threads; unchanged files are reused', flush=True)
    started = time.monotonic()
    process = subprocess.Popen(command)
    while True:
        try:
            code = process.wait(timeout=REPORT_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            print(f'[COPY {source.name}] Robocopy active: {time.monotonic() - started:.0f}s', flush=True)
    # Robocopy uses 0..7 for successful copies/skips and >=8 for failure.
    if code >= 8:
        raise RuntimeError(f'Robocopy failed with exit code {code}: {source}')
    print(f'[COPY {source.name}] 100.0% | {len(files):,}/{len(files):,} files checked | {time.monotonic() - started:.1f}s | Robocopy code {code}', flush=True)


def create_zip(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    if source == destination or source in destination.parents:
        raise ValueError('ZIP must be outside the directory being archived')
    files = collect_files(source, runtime=True)
    progress = Progress('ZIP', files)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path, size in files:
            name = (Path(source.name) / path.relative_to(source)).as_posix()
            progress.report(name, force=size >= 64 * 1024**2)
            with path.open('rb') as reader, archive.open(name, 'w', force_zip64=True) as writer:
                progress.transfer(reader, writer, name)
    progress.report(f'complete: {destination} ({destination.stat().st_size / 1024**2:.1f} MiB)', force=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['copy', 'zip'])
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--runtime', action='store_true')
    args = parser.parse_args()
    if args.operation == 'copy':
        copy_tree(args.source, args.destination, runtime=args.runtime)
    else:
        create_zip(args.source, args.destination)
