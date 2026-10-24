"""Installed lclang command entrance behavior."""

import contextlib
import io
import runpy
from unittest.mock import patch

from tests.support import FilesystemCase
from yambl_compiler.application.cli import main


class CliTests(FilesystemCase):
    def test_undefined_dictionary_name_fails_but_quoted_identifier_warns(self):
        source = self.write('fund.yambl', '')
        for spelling, status, severity in [
            ('PortfolioGroupTypes', 1, 'error'),
            ("'PortfolioGroupTypes'", 0, 'warning'),
        ]:
            source.write_text(
                '\n' * 10
                + 'entity PortfolioGroup {\n    attr category: code<'
                + spelling
                + '>\n}\n',
                encoding='utf-8',
            )
            with (
                self.subTest(spelling=spelling),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    main(['python', 'yambl.py', 'check', '-o', 'path', str(source)]), status
                )
            self.assertIn(f'{severity}: undefined Decodes: PortfolioGroupTypes', output.getvalue())
            self.assertIn('fund.yambl:12:', output.getvalue())

    def test_cli_real_entrance(self):
        source = self.write('a.yambl', 'entity A{}')
        for command, options, expected in [
            ('check', [], 0),
            ('format', ['-o', 'check'], 1),
            ('format', ['--dryrun'], 1),
            ('format', [], 0),
            ('format', ['-o', 'check'], 0),
        ]:
            with self.subTest(command=command, options=options):
                before = source.read_bytes()
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    status = main(
                        ['python', 'yambl.py', command, *options, '-o', 'path', str(source)]
                    )
                self.assertEqual(status, expected)
                self.assertIn('file(s)', output.getvalue())
                if options:
                    self.assertEqual(source.read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['python', 'yambl.py', 'format', '--help']), 0)
        self.assertIn('path', output.getvalue())
        with (
            patch('sys.argv', ['yambl', '--version']),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_module('yambl_compiler', run_name='__main__')
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn('0.1.0', output.getvalue())
