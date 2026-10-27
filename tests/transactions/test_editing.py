"""Verify isolation and table loading for mutable transaction candidates."""

from tests.storage.test_runtime import SOURCE
from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.transactions.incremental import IncrementalData
from yambl_compiler.transactions.snapshot import SnapshotBackend


class CandidateLoadingTests(FilesystemCase):
    """Check that a candidate owns detached rows from a pinned snapshot."""

    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', SOURCE)
        self.layout = build_layout(compile_repository(self.root))
        self.tables = {table.name: [] for table in self.layout.tables}
        a = next(table for table in self.layout.tables if table.name == 'tb_a')
        self.tables['tb_a'] = [
            {
                column.name: (
                    'A'
                    if column.name == 'entity_type'
                    else (
                        'a'
                        if column.name == 'id'
                        else 'NO_ENTRY' if column.name.endswith('__state') else None
                    )
                )
                for column in a.columns
            }
        ]

    def test_candidate_loads_only_requested_table_and_detaches_rows(self):
        class CountingBackend(SnapshotBackend):
            def __init__(self, tables):
                super().__init__(tables)
                self.loaded = []
                self.closed = False

            def rows(self, table, keys):
                self.loaded.append(table)
                return super().rows(table, keys)

            def close(self):
                self.closed = True

        backend = CountingBackend(self.tables)
        candidate = IncrementalData(self.layout, backend)
        self.assertEqual(backend.loaded, [])
        entity = candidate.catalog.entity('A')
        self.assertEqual(candidate.actual(entity, 'a').definition.identity, 'A')
        self.assertEqual(backend.loaded, ['tb_a'])
        candidate.select('tb_a', {'id': 'a'})[0]['text__state'] = 'EMPTY'
        self.assertEqual(self.tables['tb_a'][0]['text__state'], 'NO_ENTRY')
        candidate.insert(entity, 'b')
        with self.assertRaisesRegex(ValueError, 'entity ID already exists'):
            candidate.insert(entity, 'b')
        self.assertEqual(len(self.tables['tb_a']), 1)
        self.assertEqual(backend.loaded, ['tb_a', 'tb_a'])
        backend.close()
        self.assertTrue(backend.closed)
