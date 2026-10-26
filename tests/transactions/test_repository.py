"""Exercise unified configured data access and durable transaction exports."""

import unittest
from unittest.mock import patch

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Success
from yambl_compiler.transactions.model import Group, Operation, Plan
from yambl_compiler.transactions.repository import DataStore
from yambl_compiler.transactions.syntax import parse
from yambl_compiler.transactions.writing import dumps


class RepositoryTests(CatalogCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.module = load_generated(output / 'sample_model', 'sample_model')

        class Store(DataStore):
            reader_type = self.module.SampleReader
            schema_path = output / 'schema.sql'

        self.store_type = Store
        self.plan = Plan(
            'demo',
            self.catalog.layout.model.model_digest,
            (
                Group(
                    'entities', (Operation('insert', 'A', ('key', 'text'), (('a', 'original'),)),)
                ),
            ),
        )
        self.plan_path = self.write('demo.yaplan', dumps(self.plan))

    async def configured(self, backend, suffix=''):
        config = self.write(
            f'{backend}{suffix}.lclcfg', f"backend: '{backend}'\npath: '{backend}{suffix}.data'\n"
        )
        return await self.store_type.create(config), config

    async def test_same_interface_and_pinned_reader_for_both_backends(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, config = await self.configured(backend)
                self.addCleanup(store.close)
                initial = store.apply(self.plan_path)
                self.assertEqual(initial.status, 'committed')
                self.assertTrue(initial.path.is_file())
                old_reader = store.read()
                self.addCleanup(old_reader.close)
                patch_plan = Plan(
                    'change',
                    self.plan.model,
                    (
                        Group(
                            'labels', (Operation('update', 'A', ('key', 'text'), (('a', 'new'),)),)
                        ),
                    ),
                )
                path = self.write('change.yaplan', dumps(patch_plan))
                changed = store.apply(path)
                reader = store.read()
                self.addCleanup(reader.close)
                self.assertEqual(reader.get_a('a').value.text, Success('new'))
                self.assertEqual(old_reader.get_a('a').value.text, Success('original'))
                reverted = store.rollback(changed.path)
                self.assertEqual(reverted.status, 'committed')
                restored = store.read()
                self.addCleanup(restored.close)
                self.assertEqual(restored.get_a('a').value.text, Success('original'))
                self.assertEqual(parse(reverted.path.read_text()).source, changed.id)
                reopened = await self.store_type.open(config)
                reopened.close()
                with self.assertRaises(FileExistsError):
                    await self.store_type.create(config)

    async def test_export_failure_is_committed_and_recovers_without_reapply(self):
        for backend in ('sqlite', 'files'):
            store, config = await self.configured(backend)
            self.addCleanup(store.close)
            with patch.object(store, 'export', side_effect=OSError('publication failed')):
                report = store.apply(self.plan_path)
            self.assertEqual(report.status, 'publication_pending')
            self.assertFalse(report.path.exists())
            reopened = await self.store_type.open(config)
            self.addCleanup(reopened.close)
            self.assertEqual(parse(report.path.read_text()).id, report.id)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                reopened.apply(self.plan_path)
            self.assertEqual(reopened.export(report.id), report.path)

    async def test_config_types_paths_and_closed_facade(self):
        for text in ("backend: 'unknown'\npath: 'data'", "backend: 'sqlite'\npath: 1"):
            with self.assertRaises(ValueError):
                await self.store_type.configured(self.write('bad.lclcfg', text))
        store, _ = await self.configured('files')
        self.assertEqual(store.path, self.root / 'files.data')
        with store:
            self.assertIs(store.__enter__(), store)
        with self.assertRaisesRegex(ValueError, 'closed'):
            store.read()

    async def test_precommit_failure_leaves_data_unchanged(self):
        for backend in ('sqlite', 'files'):
            store, _ = await self.configured(backend)
            self.addCleanup(store.close)
            before = store.snapshot().tables
            invalid = Plan(
                'invalid',
                self.plan.model,
                (
                    Group(
                        'all',
                        (
                            Operation('insert', 'A', ('key',), (('a',),)),
                            Operation('update', 'A', ('key', 'count'), (('a', 'wrong'),)),
                        ),
                    ),
                ),
            )
            with self.assertRaisesRegex(ValueError, 'integer'):
                store.apply(self.write('invalid.yaplan', dumps(invalid)))
            self.assertEqual(store.snapshot().tables, before)
