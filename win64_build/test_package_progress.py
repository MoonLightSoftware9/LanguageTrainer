"""Check package data, ZIP layout and reported byte totals."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('progress_checks', Path(__file__).with_name('package_progress.py'))
progress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress)


class PackagingProgressTests(unittest.TestCase):
    def test_copy_and_zip_preserve_bytes_and_report_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            (source / 'nested').mkdir(parents=True)
            (source / '__pycache__').mkdir()
            payload = bytes(range(256)) * 20000
            (source / 'nested/payload.bin').write_bytes(payload)
            (source / 'name with spaces.txt').write_text('test')
            (source / '__pycache__/ignored.pyc').write_bytes(b'ignored')
            destination = root / 'LanguageTrainer'
            output = io.StringIO()
            with redirect_stdout(output):
                progress.copy_tree(source, destination, ignore=('__pycache__',))
                progress.create_zip(destination, root / 'package.zip')
            self.assertEqual((destination / 'nested/payload.bin').read_bytes(), payload)
            self.assertFalse((destination / '__pycache__').exists())
            with zipfile.ZipFile(root / 'package.zip') as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read('LanguageTrainer/nested/payload.bin'), payload)
                self.assertEqual(archive.read('LanguageTrainer/name with spaces.txt'), b'test')
            self.assertIn('100.0%', output.getvalue())
            self.assertIn('2/2 files', output.getvalue())
            self.assertIn('[ZIP]', output.getvalue())

    def test_missing_directory_and_recursive_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for operation in (progress.copy_tree, progress.create_zip):
                with self.assertRaises(FileNotFoundError):
                    operation(root / 'missing', root / 'out')
                with self.assertRaises(ValueError):
                    operation(root, root / 'recursive-output')


if __name__ == '__main__':
    unittest.main()
