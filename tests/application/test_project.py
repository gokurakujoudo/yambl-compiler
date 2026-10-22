"""Repository discovery and atomic formatting publication."""

import os
from pathlib import Path
from unittest.mock import patch

from tests.support import FilesystemCase
from yambl_compiler.application.project import replace_text, run, source_files


class ProjectTests(FilesystemCase):
    def test_inaccessible_directory_stops_batch(self):
        source = self.write('a.yambl', 'entity A{}')
        blocked = self.write('restricted/b.yambl', 'entity B{}').parent
        original = os.scandir

        def scan(path):
            if Path(path) == blocked:
                raise PermissionError('directory denied')
            return original(path)

        with patch('os.scandir', side_effect=scan):
            report = run(self.root, 'format')
        self.assertFalse(report.ok)
        self.assertEqual(report.messages, ['directory denied'])
        self.assertEqual(source.read_bytes(), b'entity A{}')

    def test_file_and_repository_checks(self):
        source = self.write('core/fund.yambl', 'namespace core entity Fund { attr h: Holding }')
        self.assertFalse(run(source, 'check').ok)
        self.write('core/holding.yambl', 'namespace core entity Holding { id x: snowflake_id }')
        self.write('yambl-root.lclcfg', "prefix: ''")
        self.write('.git/bad.yambl', '?')
        self.write('venv/bad.yambl', '?')
        self.write('README.md', '?')
        result = run(source, 'check')
        self.assertTrue(result.ok, result.messages)
        self.assertEqual(result.messages[-1], 'checked 2 file(s)')
        self.assertEqual(result.messages, ['checked 2 file(s)'])
        self.assertEqual(len(source_files(source, False)), 1)
        self.assertTrue(run(self.root, 'check').ok)

    def test_format_drift_and_bytes(self):
        source = self.write('a.yambl', '\ufeffentity A{ attr x:string }\r\n\r\n\r\n')
        before = source.read_bytes()
        report = run(source, 'format', verify=True)
        self.assertFalse(report.ok)
        self.assertIn('would format:', report.messages[0])
        self.assertEqual(source.read_bytes(), before)
        self.assertTrue(run(source, 'format').ok)
        expected = b'entity A {\n    attr x: string\n}\n'
        self.assertEqual(source.read_bytes(), expected)
        self.assertTrue(run(source, 'format', verify=True).ok)
        self.assertEqual(run(source, 'format').messages, ['formatted 0 of 1 file(s)'])
        self.assertEqual(list(self.root.glob('.yambl-*')), [])

    def test_syntax_error_prevents_all_writes(self):
        first = self.write('a.yambl', 'entity A{}')
        bad = self.write('b.yambl', 'entity B')
        report = run(self.root, 'format')
        self.assertFalse(report.ok)
        self.assertIn(str(bad), report.messages[0])
        self.assertEqual(first.read_text(), 'entity A{}')

    def test_path_encoding_and_io_errors(self):
        self.assertIn('does not exist', run(self.root / 'absent', 'check').messages[0])
        self.assertIn('no .yambl', run(self.root, 'check').messages[0])
        source = self.write('other.txt', 'text')
        self.assertIn('expected a .yambl', run(source, 'check').messages[0])
        source = self.write('bad.yambl', '')
        source.write_bytes(b'\xff')
        self.assertFalse(run(source, 'check').ok)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('read denied')):
            self.assertEqual(run(source, 'format').messages, ['read denied'])

    def test_atomic_write_failure_preserves_original(self):
        source = self.write('a.yambl', 'entity A{}')
        for operation in ['os.replace', 'pathlib.Path.write_text']:
            with (
                self.subTest(operation=operation),
                patch(operation, side_effect=PermissionError('write denied')),
            ):
                report = run(source, 'format')
            self.assertFalse(report.ok)
            self.assertIn('write denied', report.messages[-1])
            self.assertEqual(source.read_bytes(), b'entity A{}')
            self.assertEqual(list(self.root.glob('.yambl-*')), [])
        replace_text(source, 'entity A {}\n\n')
        self.assertEqual(source.read_bytes(), b'entity A {}\n\n')

    def test_symlink_scan(self):
        source = self.write('a.yambl', 'entity A {}')
        # File symlinks require Windows developer mode; inject only the filesystem predicate.
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == source or original(path)):
            with self.assertRaisesRegex(ValueError, 'no .yambl'):
                source_files(self.root, False)
