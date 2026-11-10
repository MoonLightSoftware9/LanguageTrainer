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
    def run_desktop(self, mode):
        urls, errors, processes = [], [], []
        class Event:
            callback = None
            def __iadd__(self, callback):
                self.callback = callback
                return self
        event = Event()
        window = types.SimpleNamespace(events=types.SimpleNamespace(closed=event), load_url=urls.append, load_html=errors.append)
        def spawn(*args, **kwargs):
            if mode == 'error':
                raise OSError('backend missing')
            process = Mock()
            process.poll.return_value = None
            def output():
                yield 'DICT_SERVER_READY {"url":"http://127.0.0.1:43210/"}\n'
                event.callback()
            process.stdout = output()
            processes.append((process, args, kwargs))
            return process
        def start(function, **kwargs):
            self.assertEqual(kwargs['gui'], 'edgechromium')
            if mode == 'close_before_start':
                event.callback()
            function()
        fake = types.SimpleNamespace(create_window=lambda *a, **kw: window, start=start)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, DICT_PORTABLE_ROOT=folder), patch.dict(sys.modules, webview=fake), patch.object(desktop.subprocess, 'Popen', side_effect=spawn), patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
            self.assertEqual(desktop.main(), 0)
            sys.stdout.close()
        return urls, errors, processes

    def test_readiness_and_close_terminate_backend(self):
        urls, errors, processes = self.run_desktop('normal')
        self.assertEqual(urls, ['http://127.0.0.1:43210/'])
        self.assertEqual(errors, [])
        process, args, kwargs = processes[0]
        process.terminate.assert_called()
        self.assertIn('--port', args[0])
        self.assertEqual(kwargs['env']['PYTHONUNBUFFERED'], '1')

    def test_startup_failure_is_visible(self):
        urls, errors, processes = self.run_desktop('error')
        self.assertEqual(urls, [])
        self.assertIn('backend missing', errors[0])

    def test_closed_window_does_not_launch_backend(self):
        self.assertEqual(self.run_desktop('close_before_start'), ([], [], []))


if __name__ == '__main__':
    unittest.main()
