"""Exercise entity-key checks through the real reader and detached snapshots."""

import sqlite3
import unittest
from contextlib import closing

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.entity_validation import validate_entities
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.rendering import ddl
from yambl_compiler.storage.results import Success
from yambl_compiler.storage.runtime import EntityHandle, Reader
from yambl_compiler.transactions.snapshot import SnapshotBackend


class EntityValidationTests(FilesystemCase):
    def test_explicit_validation_lists_all_conflicting_instances(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl', '@unique_by(self.code) entity A { id id: string attr code: string }'
        )
        layout = build_layout(compile_repository(self.root))
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript(ddl(layout))
            db.executemany(
                'INSERT INTO tb_a(id, code__state, code) VALUES (?, 0, ?)',
                [('x', 'same'), ('y', 'same'), ('z', 'same')],
            )
            tables = {
                table.name: [dict(row) for row in db.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }

        class ModelReader(Reader):
            entity_types = {'A': EntityHandle}

        with ModelReader(SnapshotBackend(tables), layout) as reader:
            report = reader.validate()
        self.assertIsInstance(report, Success)
        matches = [issue for issue in report.value.issues if 'unique_by' in issue.message]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].ids, ('A:x', 'A:y', 'A:z'))
        self.assertIn('model.yambl:1:', matches[0].message)

    def test_error_is_reported_even_after_an_earlier_missing_value(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '@unique_by(self.first, self.second) entity A { id id: string attr first: string attr second: integer }',
        )
        layout = build_layout(compile_repository(self.root))
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript(ddl(layout))
            db.execute(
                "INSERT INTO tb_a(id, first__state, second__state, second) VALUES ('x', 1, 0, 'bad')"
            )
            tables = {
                table.name: [dict(row) for row in db.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }

        class ModelReader(Reader):
            entity_types = {'A': EntityHandle}

        with ModelReader(SnapshotBackend(tables), layout) as reader:
            issues = validate_entities(reader)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].field, 'A.second')
        self.assertEqual(issues[0].ids, ('A:x',))
        self.assertIn('unique_by read failed', issues[0].message)


if __name__ == '__main__':
    unittest.main()
