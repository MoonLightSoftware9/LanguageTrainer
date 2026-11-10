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
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for pattern in ("python*.exe", "*.dll", "python*.zip", "LICENSE.txt"):
        for path in base.glob(pattern):
            shutil.copy2(path, destination / path.name)
    for name in ("Lib", "DLLs"):
        copy_tree(base / name, destination / name, ignore=("site-packages", "__pycache__"))
    copy_tree(Path(sys.prefix) / "Lib/site-packages", destination / "Lib/site-packages",
              ignore=("__pycache__",))
    for name in ("dict.py", "get_sounds.py", "lookup_parse.py", "translate.py"):
        shutil.copy2(source / name, destination / name)
    shutil.copytree(source / "dictapp", destination / "dictapp",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(Path(__file__).with_name("desktop.py"), destination / "desktop.py")
    make_launcher(destination.parent / "LanguageTrainer.exe")
    print(f"Python runtime and sources: {destination}")


def make_launcher(destination):
    """Use distlib's prebuilt GUI launcher with a relocatable interpreter path."""
    bootstrap = b"""import sys
from pathlib import Path

from package_progress import copy_tree
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
