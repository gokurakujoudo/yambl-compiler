"""Compare affected reference-cycle checks with complete candidate validation."""

import sqlite3
import unittest
from datetime import date, timedelta
from random import Random

from tests.storage.test_validation import SOURCE
from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.rendering import ddl
from yambl_compiler.storage.results import EntityRef, NonNormalResult, Status, Success
from yambl_compiler.storage.runtime import Reader
from yambl_compiler.storage.validation import cycles, validate_data
from yambl_compiler.transactions.incremental_validation import _periods, validate_affected_cycles
from yambl_compiler.transactions.snapshot import SnapshotBackend


class IncrementalCycleTests(FilesystemCase):
    """Exercise real field decoding and identity resolution with candidate rows."""

    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', SOURCE)
        self.layout = build_layout(compile_repository(self.root))
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(ddl(self.layout))
        self.db.executemany(
            'INSERT INTO tb_node(id, entity_type) VALUES (?, ?)', [('a', 'Node'), ('b', 'Child')]
        )

    def verify(self, changed, expected):
        """Compare candidate cycle diagnostics with the full scan."""
        tables = {
            table.name: [dict(row) for row in self.db.execute(f'SELECT * FROM "{table.name}"')]
            for table in self.layout.tables
        }
        reader = Reader(SnapshotBackend(tables), self.layout)
        self.addCleanup(reader.close)
        select = reader._backend.rows
        field = reader._batch.implementation
        complete = validate_data(self.layout, tables, field, reader._value).value
        incremental = validate_affected_cycles(self.layout, select, field, reader._value, changed)
        full_cycles = [
            issue for issue in complete.issues if issue.message == 'same-field reference cycle'
        ]
        self.assertEqual(bool(full_cycles), expected)
        self.assertEqual(bool(incremental.issues), expected)
        if expected:
            self.assertEqual(incremental.issues, tuple(full_cycles))

    def test_inserted_identity_activates_a_dangling_edge(self):
        self.db.execute("UPDATE tb_node SET parent__state='VALUE', parent='new' WHERE id='a'")
        self.db.execute(
            "INSERT INTO tb_node(id, entity_type, parent__state, parent) VALUES ('new', 'Node', 'VALUE', 'a')"
        )
        self.verify({'tb_node': [{'id': 'new', 'entity_type': 'Node'}]}, True)

    def test_subtype_mismatch_stays_dangling(self):
        self.db.execute("UPDATE tb_node SET narrow__state='VALUE', narrow='a' WHERE id='a'")
        self.verify({'tb_node': [{'id': 'a'}]}, False)
        self.db.execute("UPDATE tb_node SET narrow__state='VALUE', narrow='b' WHERE id='b'")
        self.verify({'tb_node': [{'id': 'b'}]}, True)

    def test_scd_intersection_and_exclusive_end(self):
        self.db.executemany(
            'INSERT INTO tb_node_period(id,valid_from,valid_to,period__state,period) VALUES (?,?,?,?,?)',
            [
                ('a', '2026-01-01', '2026-01-03', 'VALUE', 'b'),
                ('b', '2026-01-03', '2026-01-05', 'VALUE', 'a'),
            ],
        )
        self.verify({'tb_node_period': [{'id': 'a'}]}, False)
        self.db.execute("UPDATE tb_node_period SET valid_from='2026-01-02' WHERE id='b'")
        self.verify({'tb_node_period': [{'id': 'b'}]}, True)

    def test_collection_members(self):
        self.db.execute("UPDATE tb_node SET children__state='VALUE' WHERE id IN ('a','b')")
        self.db.executemany('INSERT INTO tb_node_children VALUES (?, ?)', [('a', 'b'), ('b', 'a')])
        self.verify({'tb_node_children': [{'id': 'b', 'member_id': 'a'}]}, True)

    def test_dimensions_and_snapshot_dates_stay_distinct(self):
        self.db.executemany(
            'INSERT INTO tb_node_static_source(id,dim_source,dim__state,dim) VALUES (?,?,?,?)',
            [('a', 'X', 'VALUE', 'b'), ('b', 'Y', 'VALUE', 'a')],
        )
        self.db.executemany(
            'INSERT INTO tb_node_snap(id,date,snap__state,snap) VALUES (?,?,?,?)',
            [('a', '2026-01-01', 'VALUE', 'b'), ('b', '2026-01-02', 'VALUE', 'a')],
        )
        self.verify({'tb_node_static_source': [{'id': 'a'}], 'tb_node_snap': [{'id': 'a'}]}, False)
        self.db.execute("UPDATE tb_node_static_source SET dim_source='X' WHERE id='b'")
        self.verify({'tb_node_static_source': [{'id': 'b'}]}, True)

    def test_changed_identity_may_have_been_deleted(self):
        self.verify({'tb_node': [{'id': 'missing'}]}, False)

    def test_only_reachable_identities_are_selected(self):
        self.db.executemany(
            'INSERT INTO tb_node(id,entity_type,parent__state,parent) VALUES (?,?,?,?)',
            [('c', 'Node', 'VALUE', 'd'), ('d', 'Node', 'VALUE', 'c')],
        )
        self.db.execute("UPDATE tb_node SET parent__state='VALUE', parent='b' WHERE id='a'")
        tables = {
            table.name: [dict(row) for row in self.db.execute(f'SELECT * FROM "{table.name}"')]
            for table in self.layout.tables
        }
        reader = Reader(SnapshotBackend(tables), self.layout)
        self.addCleanup(reader.close)
        selected = []

        def select(table, keys):
            selected.append((table, dict(keys)))
            return reader._backend.rows(table, keys)

        field = reader._batch.implementation
        full = validate_data(self.layout, tables, field, reader._value).value
        self.assertFalse(full.valid)
        result = validate_affected_cycles(
            self.layout, select, field, reader._value, {'tb_node': [{'id': 'a'}]}
        )
        self.assertTrue(result.valid)
        self.assertEqual({keys['id'] for table, keys in selected if table == 'tb_node'}, {'a', 'b'})

    def test_other_root_reference_is_not_a_same_tree_cycle(self):
        self.db.execute("INSERT INTO tb_other(id,entity_type) VALUES ('x','Other')")
        self.db.execute(
            'UPDATE tb_node SET foreign__state=\'VALUE\', "foreign"=\'x\' WHERE id=\'a\''
        )
        self.verify({'tb_node': [{'id': 'a'}]}, False)

    def test_invalid_identity_and_unreadable_field_fail_closed(self):
        tables = {
            table.name: [dict(row) for row in self.db.execute(f'SELECT * FROM "{table.name}"')]
            for table in self.layout.tables
        }
        reader = Reader(SnapshotBackend(tables), self.layout)
        self.addCleanup(reader.close)
        original = reader._backend.rows
        field = reader._batch.implementation
        changed = {'tb_node': [{'id': 'a'}]}

        def duplicate(table, keys):
            rows = original(table, keys)
            return rows * 2 if table == 'tb_node' and keys == {'id': 'a'} else rows

        def bad_type(table, keys):
            rows = original(table, keys)
            if table == 'tb_node' and keys == {'id': 'a'}:
                rows[0]['entity_type'] = 'Other'
            return rows

        for select, message in ((duplicate, 'duplicate identity'), (bad_type, 'invalid actual')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_affected_cycles(self.layout, select, field, reader._value, changed)

        def unreadable(stored, declaration, row):
            if declaration.identity == 'Node.parent':
                return NonNormalResult(Status.ERROR, 'invalid reference')
            return reader._value(stored, declaration, row)

        with self.assertRaisesRegex(ValueError, 'invalid reference'):
            validate_affected_cycles(self.layout, original, field, unreadable, changed)

        def absent(actual, identity):
            return None if identity == 'Node.parent' else field(actual, identity)

        def nonreference(stored, declaration, row):
            if declaration.identity == 'Node.parent':
                return Success('not a reference')
            return reader._value(stored, declaration, row)

        self.assertTrue(
            validate_affected_cycles(self.layout, original, absent, reader._value, changed).valid
        )
        self.assertTrue(
            validate_affected_cycles(self.layout, original, field, nonreference, changed).valid
        )

    def test_period_sweep_retains_duplicate_edges_and_half_open_boundaries(self):
        self.assertEqual(list(_periods([('a', 'b', '2026-01-01', '2026-01-03')], '')), [])
        records = [
            ('a', 'b', '2026-01-01', '2026-01-04'),
            ('a', 'b', '2026-01-02', '2026-01-05'),
            ('b', 'a', '2026-01-03', '2026-01-06'),
        ]
        self.assertEqual(
            list(_periods(records, '')),
            [
                ('2026-01-01', '2026-01-02', {('a', 'b')}),
                ('2026-01-02', '2026-01-03', {('a', 'b')}),
                ('2026-01-03', '2026-01-04', {('a', 'b'), ('b', 'a')}),
                ('2026-01-04', '2026-01-05', {('a', 'b'), ('b', 'a')}),
                ('2026-01-05', '2026-01-06', {('b', 'a')}),
            ],
        )

    def test_period_sweep_matches_brute_force_reference(self):
        random = Random(1729)
        for case in range(200):
            records = []
            for _ in range(random.randrange(1, 16)):
                start = random.randrange(1, 8)
                end = random.randrange(start + 1, 10)
                records.append((random.choice('abcd'), random.choice('abcd'), str(start), str(end)))
            boundaries = sorted({value for _, _, start, end in records for value in (start, end)})
            expected = []
            for start, end in zip(boundaries, boundaries[1:]):
                active = {(a, b) for a, b, low, high in records if low <= start < high}
                witnesses = cycles(active)
                if witnesses:
                    expected.append((start, end, witnesses))
            observed = [
                (start, end, witnesses)
                for start, end, edges in _periods(records, '')
                if (witnesses := cycles(edges))
            ]
            with self.subTest(case=case):
                self.assertEqual(observed, expected)

    def test_long_reference_ring_is_iterative_and_selected_by_id(self):
        count = 10000
        rows = {
            f'{i:05}': {'id': f'{i:05}', 'entity_type': 'Node', 'parent': f'{(i + 1) % count:05}'}
            for i in range(count)
        }
        lookups = []

        def select(table, keys):
            lookups.append((table, dict(keys)))
            row = rows.get(keys['id']) if table == 'tb_node' else None
            return [row] if row is not None else []

        def field(actual, identity):
            return self.layout.fields[identity].field if identity == 'Node.parent' else None

        def read(stored, declaration, row):
            return Success(EntityRef('Node', row['parent']))

        report = validate_affected_cycles(
            self.layout, select, field, read, {'tb_node': [{'id': '00000'}]}
        )
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(len(report.issues[0].ids), count + 1)
        self.assertEqual(report.issues[0].ids[0], '00000')
        self.assertEqual(report.issues[0].ids[-1], '00000')
        self.assertLess(len(lookups), count * 2 + 20)
        self.assertTrue(all('id' in keys for _, keys in lookups))

    def test_long_temporal_ring_without_intersection_has_no_cycle(self):
        count = 4000
        rows = {f'{i:05}': {'id': f'{i:05}', 'entity_type': 'Node'} for i in range(count)}
        periods = {
            f'{i:05}': {
                'id': f'{i:05}',
                'valid_from': (date(2000, 1, 1) + timedelta(days=2 * i)).isoformat(),
                'valid_to': (date(2000, 1, 1) + timedelta(days=2 * i + 1)).isoformat(),
                'period': f'{(i + 1) % count:05}',
            }
            for i in range(count)
        }

        def select(table, keys):
            row = (rows if table == 'tb_node' else periods).get(keys['id'])
            return [row] if row is not None else []

        def field(actual, identity):
            return self.layout.fields[identity].field if identity == 'Node.period' else None

        def read(stored, declaration, row):
            return Success(EntityRef('Node', row['period']))

        report = validate_affected_cycles(
            self.layout, select, field, read, {'tb_node_period': [{'id': '00000'}]}
        )
        self.assertTrue(report.valid)


if __name__ == '__main__':
    unittest.main()
