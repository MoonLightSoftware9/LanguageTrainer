"""Stage a Windows Python runtime and plain application sources, without compilation."""
import io
from importlib.resources import files
import zipfile
import shutil
import sys
from pathlib import Path

from package_progress import copy_tree


def package(source, destination):
    if sys.platform != "win32":
        raise SystemExit("Run this helper with the build virtual environment on Windows")
    base = Path(sys.base_prefix)
    destination.mkdir(parents=True, exist_ok=True)
    for pattern in ("python*.exe", "*.dll", "python*.zip", "LICENSE.txt"):
        for path in base.glob(pattern):
            shutil.copy2(path, destination / path.name)
    for name in ("Lib", "DLLs"):
        copy_tree(base / name, destination / name, ignore=("site-packages", "__pycache__"), runtime=True)
    copy_tree(Path(sys.prefix) / "Lib/site-packages", destination / "Lib/site-packages",
              ignore=("__pycache__", "webview", "pythonnet", "clr_loader", "clr.py"), runtime=True)
    for name in ("dict.py", "get_sounds.py", "lookup_parse.py", "translate.py"):
        shutil.copy2(source / name, destination / name)
    shutil.copytree(source / "dictapp", destination / "dictapp",
                    ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    shutil.copy2(Path(__file__).with_name("desktop.py"), destination / "desktop.py")
    # Wheels provide these redistributable DLLs. Copy them beside both Python
    # executables so the backend also works without a system C++ runtime.
    runtime_names = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
                     "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
                     "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll", "concrt140.dll")
    for directory in (destination / "Lib/site-packages/PySide6", Path(sys.prefix), Path(sys.prefix) / "Scripts"):
        for name in runtime_names:
            if (directory / name).is_file():
                shutil.copy2(directory / name, destination / name)
    (destination / "python312._pth").write_text(".\nDLLs\nLib\nLib/site-packages\npython312.zip\nimport site\n")
    make_launcher(destination.parent / "LanguageTrainer.exe")
    print(f"Python runtime and sources: {destination}")


def make_launcher(destination):
    """Use distlib's prebuilt GUI launcher with a relocatable interpreter path."""
    bootstrap = b"""import sys
from pathlib import Path
root = Path(sys.argv[0]).resolve().parent
sys.path.insert(0, str(root / 'backend'))
from desktop import main
raise SystemExit(main())
"""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", bootstrap)
    stub = files("distlib").joinpath("w64.exe").read_bytes()
    shebang = b"#!<launcher_dir>\\backend\\pythonw.exe\n"
    destination.write_bytes(stub + shebang + stream.getvalue())


if __name__ == "__main__":
    package(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
