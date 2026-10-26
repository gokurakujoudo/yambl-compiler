"""Verify native workflow commands against configured data stores."""

import asyncio
import contextlib
import io
import json
from unittest.mock import patch

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success
from yambl_compiler.transactions.cli import main
from yambl_compiler.transactions.model import Group, Operation, Plan
from yambl_compiler.transactions.writing import dumps


class DataCliTests(CatalogCase):
    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.api = load_generated(output / 'sample_model', 'sample_model')
        operation = Operation('insert', 'A', ('key', 'text'), (('a', 'original'),))
        plan = Plan('demo', self.catalog.layout.model.model_digest, (Group('all', (operation,)),))
        self.plan = self.write('demo.yaplan', dumps(plan))

    def invoke(self, command, config, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(self.api.SampleData, [command, '-o', 'config', str(config), *arguments])
        reports = [
            json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')
        ]
        return code, reports, output.getvalue()

    def config(self, backend):
        return self.write(f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n")

    def test_same_cli_lifecycle_for_both_backends(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.config(backend)
                code, reports, output = self.invoke('create', config)
                self.assertEqual(code, 0, output)
                self.assertEqual(reports, [{'status': 'created'}])
                code, reports, output = self.invoke('apply', config, '-o', 'source', str(self.plan))
                self.assertEqual(code, 0, output)
                self.assertEqual(reports[0]['status'], 'committed')
                transaction = reports[0]['path']
                code, reports, output = self.invoke('check-data', config)
                self.assertEqual(code, 0, output)
                self.assertTrue(reports[0]['valid'])
                code, reports, output = self.invoke(
                    'rollback', config, '-o', 'source', transaction, '-o', 'groups', "LCL[['all']]"
                )
                self.assertEqual(code, 0, output)
                self.assertEqual(reports[0]['status'], 'committed')
                with asyncio.run(self.api.SampleData.open(config)) as data:
                    reader = data.read()
                    self.assertEqual(reader.get_a('a').status, Status.NO_ENTRY)
                    reader.close()

    def test_dryrun_does_not_create_mutate_or_recover_files(self):
        config = self.config('files')
        code, reports, output = self.invoke('create', config, '--dryrun')
        self.assertEqual(code, 0, output)
        self.assertEqual(reports, [{'status': 'dry_run', 'operation': 'create'}])
        self.assertFalse((self.root / 'files.data').exists())
        with asyncio.run(self.api.SampleData.create(config)) as data:
            report = data.apply(self.plan)
            external = self.write('external.yatran', report.path.read_text())
            report.path.unlink()
            before = {
                p.relative_to(data.path): p.read_bytes()
                for p in data.path.rglob('*')
                if p.is_file()
            }
            for command, source in (
                ('apply', self.plan),
                ('rollback', external),
                ('check-data', None),
            ):
                args = ('-o', 'source', str(source)) if source else ()
                code, _, output = self.invoke(command, config, *args, '--dryrun')
                self.assertEqual(code, 0, output)
                self.assertEqual(
                    {
                        p.relative_to(data.path): p.read_bytes()
                        for p in data.path.rglob('*')
                        if p.is_file()
                    },
                    before,
                )
            code, _, _ = self.invoke('create', config, '--dryrun')
            self.assertNotEqual(code, 0)
            code, _, _ = self.invoke('rollback', config, '-o', 'source', str(self.plan), '--dryrun')
            self.assertNotEqual(code, 0)

    def test_committed_publication_failure_is_not_a_failed_apply(self):
        config = self.config('sqlite')
        self.assertEqual(self.invoke('create', config)[0], 0)
        with patch.object(self.api.SampleData, 'export', side_effect=OSError('export unavailable')):
            code, reports, output = self.invoke('apply', config, '-o', 'source', str(self.plan))
        self.assertEqual(code, 0, output)
        self.assertEqual(reports[0]['status'], 'publication_pending')
        self.assertIn('export unavailable', reports[0]['publication_error'])
        with asyncio.run(self.api.SampleData.open(config)) as data:
            reader = data.read()
            self.assertEqual(reader.get_a('a').value.text, Success('original'))
            reader.close()
        code, _, _ = self.invoke('apply', config, '-o', 'source', str(self.plan))
        self.assertNotEqual(code, 0)
