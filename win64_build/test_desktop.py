"""Checks portable launcher structure and desktop/backend lifecycle without a GUI."""
import importlib.util
from importlib.resources import files
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import zipfile


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.modules['package_progress'] = load('package_progress', 'package_progress.py')
packager = load('package_backend', 'package_backend.py')
desktop = load('desktop', 'desktop.py')


class LauncherTests(unittest.TestCase):
    def test_native_gui_launcher_and_relocated_bootstrap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'Moved folder with spaces — тест'
            root.mkdir()
            executable = root / 'LanguageTrainer.exe'
            packager.make_launcher(executable)
            data = executable.read_bytes()
            self.assertEqual(data[:2], b'MZ')
            pe = struct.unpack_from('<I', data, 0x3c)[0]
            self.assertEqual(data[pe:pe + 4], b'PE\0\0')
            self.assertEqual(struct.unpack_from('<H', data, pe + 4)[0], 0x8664)
            self.assertEqual(struct.unpack_from('<H', data, pe + 24 + 68)[0], 2)
            self.assertIn(b'#!<launcher_dir>\\backend\\pythonw.exe\n', data)
            with zipfile.ZipFile(executable) as archive:
                bootstrap = archive.read('__main__.py')
            fake_desktop = types.SimpleNamespace(main=Mock(return_value=0))
            with patch.dict(sys.modules, desktop=fake_desktop), patch.object(sys, 'argv', [str(executable)]), patch.object(sys, 'path', list(sys.path)):
                with self.assertRaises(SystemExit) as result:
                    exec(bootstrap, {'__name__': '__main__'})
                self.assertEqual(result.exception.code, 0)
                self.assertEqual(sys.path[0], str(root / 'backend'))
                fake_desktop.main.assert_called_once()

    def test_packaging_copies_interpreter_sources_and_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base, venv, source, target = [root / name for name in ('python', 'venv', 'source', 'out/backend')]
            for path in (base / 'Lib', base / 'DLLs', venv / 'Lib/site-packages', source / 'dictapp'):
                path.mkdir(parents=True)
            for name in ('python.exe', 'pythonw.exe', 'python312.dll'):
                (base / name).write_bytes(b'runtime')
            (base / 'Lib/stdlib.py').write_text('# stdlib')
            (venv / 'Lib/site-packages/dependency.py').write_text('# dependency')
            for name in ('dict.py', 'get_sounds.py', 'lookup_parse.py', 'translate.py'):
                (source / name).write_text('# application')
            with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'prefix', str(venv)), patch.object(sys, 'base_prefix', str(base)):
                packager.package(source, target)
            for name in ('python.exe', 'pythonw.exe', 'python312.dll', 'Lib/stdlib.py', 'Lib/site-packages/dependency.py', 'desktop.py', 'dict.py'):
                self.assertTrue((target / name).is_file(), name)
            self.assertTrue((target.parent / 'LanguageTrainer.exe').is_file())


class DesktopTests(unittest.TestCase):
    def test_browser_readiness_uses_serialized_json(self):
        self.assertTrue(desktop.parse_page_state('{"ready":true,"error":""}')['ready'])
        self.assertEqual(desktop.parse_page_state(None), {})
        self.assertEqual(desktop.parse_page_state('undefined'), {})
        self.assertEqual(desktop.parse_page_state('{"error":"script failed"}')['error'], 'script failed')

    def test_browser_uses_bundled_engine_and_software_rendering(self):
        with patch.dict(os.environ, {'QT_PLUGIN_PATH': 'external-install'}, clear=True):
            desktop.configure_browser()
            self.assertNotIn('QT_PLUGIN_PATH', os.environ)
            self.assertIn('--disable-gpu', os.environ['QTWEBENGINE_CHROMIUM_FLAGS'])
            self.assertEqual(os.environ['QT_QUICK_BACKEND'], 'software')

    def test_launcher_bootstrap_runs_in_fresh_isolated_interpreter(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'Folder with spaces'
            (root / 'backend').mkdir(parents=True)
            (root / 'backend/desktop.py').write_text('def main(): return 0\n')
            executable = root / 'LanguageTrainer.exe'
            packager.make_launcher(executable)
            code = "import sys,zipfile; path=sys.argv[1]; sys.argv=[path]; exec(zipfile.ZipFile(path).read('__main__.py'))"
            result = subprocess.run([sys.executable, '-I', '-c', code, str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_launch_errors_are_logged(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, DICT_PORTABLE_ROOT=folder), patch.object(desktop, 'run_window', side_effect=RuntimeError('missing browser file')), patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()), patch.object(sys, 'platform', 'linux'):
            self.assertEqual(desktop.main(), 1)
            sys.stdout.close()
            self.assertIn('missing browser file', (Path(folder) / 'data/desktop.log').read_text())

    def test_runtime_allowlist_rejects_external_frameworks_and_cpp_runtime(self):
        audit = load('audit_checks', 'runtime_audit.py')
        for name in ('kernel32.dll', 'ucrtbase.dll', 'api-ms-win-core-file-l1-1-0.dll'):
            self.assertTrue(audit.windows_dependency(name), name)
        for name in ('msvcp140.dll', 'vcruntime140.dll', 'WebView2Loader.dll', 'mscoree.dll', 'Qt6WebEngineCore.dll'):
            self.assertFalse(audit.windows_dependency(name), name)


if __name__ == '__main__':
    unittest.main()
