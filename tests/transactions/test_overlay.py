"""Exercise candidate overlays against a real, pinned SQLite snapshot."""

import json
import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

from tests.storage.test_runtime import SOURCE
from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.languages.yatran.model import Image, Transaction
from yambl_compiler.languages.yatran.writing import seal
from yambl_compiler.storage.backends import (
    FileBackend,
    Selection,
    SQLiteBackend,
    repository_contents,
)
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.rendering import ddl
from yambl_compiler.storage.results import NonNormalResult, Status
from yambl_compiler.storage.runtime import Reader
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution, prepare_rollback
from yambl_compiler.transactions.incremental import IncrementalData
from yambl_compiler.transactions.incremental_publication import prepare_files, publish_sqlite
from yambl_compiler.transactions.overlay import Overlay, RowChange


class OverlayTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', SOURCE)
        self.layout = build_layout(compile_repository(self.root))
        self.database = self.root / 'repository.sqlite'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript(ddl(self.layout))
            connection.execute(
                'CREATE TABLE tb_yambl_transactions (id TEXT PRIMARY KEY, document TEXT NOT NULL)'
            )
            connection.executemany(
                'INSERT INTO tb_a (id,entity_type) VALUES (?,?)',
                ((f'a{i:05d}', 'A') for i in range(2000)),
            )
        self.backend = SQLiteBackend(self.database, self.layout)
        self.addCleanup(self.backend.close)
        self.candidate = Overlay(self.layout, self.backend)

    def test_select_modify_insert_and_delete_stay_detached(self):
        row = self.candidate.select('tb_a', {'id': 'a00001'})[0]
        row['text__state'] = 0
        row['text'] = 'changed'
        self.assertEqual(self.candidate.select('tb_a', {'id': 'a00001'}), [row])
        self.assertEqual(self.candidate.select('tb_a', {'id': 'a00002'})[0]['text'], None)
        self.assertEqual(len(self.candidate.current['tb_a']), 2)
        self.assertEqual(self.backend.rows('tb_a', {'id': 'a00001'})[0]['text'], None)

        inserted = {**row, 'id': 'new'}
        self.candidate.add('tb_a', inserted)
        inserted['text'] = 'outside change'
        candidate_insert = self.candidate.select('tb_a', {'id': 'new'})[0]
        self.assertEqual(candidate_insert['text'], 'changed')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.candidate.add('tb_a', candidate_insert)
        self.candidate.remove('tb_a', candidate_insert)
        self.assertEqual(self.candidate.select('tb_a', {'id': 'new'}), [])
        changes = self.candidate.changes()['tb_a']
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].key, ('a00001',))
        self.assertIsNone(changes[0].before['text'])
        self.assertEqual(changes[0].after['text'], 'changed')
        changes[0].after['text'] = 'modified report'
        self.assertEqual(row['text'], 'changed')

    def test_removed_row_is_not_resurrected_and_primary_keys_cannot_change(self):
        row = self.candidate.select('tb_a', {'id': 'a00001'})[0]
        self.candidate.remove('tb_a', row)
        self.assertEqual(self.candidate.select('tb_a', {'id': 'a00001'}), [])
        deletion = self.candidate.changes()['tb_a'][0]
        self.assertEqual(deletion.before['id'], 'a00001')
        self.assertIsNone(deletion.after)
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.candidate.remove('tb_a', row)
        other = self.candidate.select('tb_a', {'id': 'a00002'})[0]
        other['id'] = 'changed-key'
        with self.assertRaisesRegex(ValueError, 'primary key changed'):
            self.candidate.changes()

    def test_incremental_candidate_matches_full_execution_and_rollback(self):
        baseline = {table.name: self.backend.rows(table.name, {}) for table in self.layout.tables}
        full = Data(self.layout, baseline)
        incremental = IncrementalData(self.layout, self.backend)
        scope = (Condition('key', 'new'),)
        plan = Plan(
            'test',
            self.layout.model.model_digest,
            (
                Group(
                    'g',
                    (
                        Operation(
                            'insert', 'A', ('key', 'text', 'members'), (('new', 'x', (1, 2)),)
                        ),
                        Operation(
                            'replace',
                            'A.price',
                            ('day', 'source', 'value'),
                            (('2026-01-01', 'One', 12),),
                            scope,
                        ),
                        Operation(
                            'replace',
                            'A.outstanding',
                            ('valid_from', 'valid_to', 'value'),
                            (('2026-01-01', '2026-02-01', 42),),
                            scope,
                        ),
                    ),
                ),
            ),
        )
        expected = Execution(full).apply(plan)
        actual = Execution(incremental).apply(plan)
        self.assertEqual(actual, expected)
        self.assertLess(len(incremental.overlay.current['tb_a']), 10)
        with Reader(incremental.overlay, self.layout) as reader:
            reader.validate_candidate(incremental)

        def assert_matches():
            changes = incremental.overlay.changes()
            for table in self.layout.tables:
                rows = {
                    tuple(row[key] for key in table.primary): row for row in baseline[table.name]
                }
                for change in changes.get(table.name, ()):
                    rows.pop(change.key, None)
                    if change.after is not None:
                        rows[change.key] = change.after
                expected_rows = {
                    tuple(row[key] for key in table.primary): row for row in full.tables[table.name]
                }
                self.assertEqual(rows, expected_rows, table.name)

        assert_matches()
        transaction = seal(
            Transaction(
                '12345678-1234-1234-1234-123456789abc', '2026-10-26T00:00:00Z', plan, actual
            )
        )
        self.assertEqual(
            prepare_rollback(incremental, transaction), prepare_rollback(full, transaction)
        )
        incremental.validate()
        full.validate()
        with Reader(incremental.overlay, self.layout) as reader:
            reader.validate_candidate(incremental)
        assert_matches()

    def test_incremental_validation_rejects_missing_collection_owners(self):
        candidate = IncrementalData(self.layout, self.backend)
        candidate.add('tb_a_members', {'id': 'missing', 'member_id': '1'})
        with self.assertRaisesRegex(ValueError, 'missing owner'):
            candidate.validate()

        candidate = IncrementalData(self.layout, self.backend)
        candidate.add('tb_a_members', {'id': 'a00001', 'member_id': '1'})
        candidate.validate()
        candidate.remove('tb_a', candidate.select('tb_a', {'id': 'a00001'})[0])
        with self.assertRaisesRegex(ValueError, 'missing owner'):
            candidate.validate()

    def test_incremental_validation_checks_scd_overlap_and_configuration(self):
        candidate = IncrementalData(self.layout, self.backend)
        candidate.add(
            'tb_a_outstanding',
            {
                'id': 'a00001',
                'valid_from': '2026-01-01',
                'valid_to': '2026-02-01',
                'outstanding__state': 0,
                'outstanding': '10',
            },
        )
        candidate.add(
            'tb_a_outstanding',
            {
                'id': 'a00001',
                'valid_from': '2026-01-15',
                'valid_to': '2026-03-01',
                'outstanding__state': 0,
                'outstanding': '20',
            },
        )
        with self.assertRaisesRegex(ValueError, 'overlapping SCD'):
            candidate.validate()

        candidate = IncrementalData(self.layout, self.backend)
        candidate.add(
            'tb_config_group', candidate.blank('tb_config_group', {'config_group_id': 'bad'})
        )
        with self.assertRaisesRegex(ValueError, 'ConfigGroup requires'):
            candidate.validate()

    def test_incremental_configuration_target_and_mapping_checks(self):
        candidate = IncrementalData(self.layout, self.backend)
        group = candidate.blank(
            'tb_config_group',
            {
                'config_group_id': 'group',
                'target_entity_type__state': 0,
                'target_entity_type': 'Missing',
                'priority__state': 0,
                'priority': '1',
                'when__state': 0,
                'then_set__state': 0,
            },
        )
        candidate.add('tb_config_group', group)
        with self.assertRaisesRegex(ValueError, 'target must be a business entity'):
            candidate.validate()
        candidate.select('tb_config_group', {'config_group_id': 'group'})[0][
            'target_entity_type'
        ] = 'A'
        candidate.validate()
        candidate.add(
            'tb_config_group_when',
            {
                'config_group_id': 'group',
                'field': 'A.text',
                'value': '{"type":{},"value":"x"}',
            },
        )
        candidate.validate()
        candidate.add(
            'tb_config_group_when',
            {
                'config_group_id': 'group',
                'field': 'A.entity_type',
                'value': '{"type":{},"value":"A"}',
            },
        )
        candidate.validate()
        candidate.add(
            'tb_config_group_when',
            {
                'config_group_id': 'group',
                'field': 'Other.label',
                'value': '{"type":{},"value":"x"}',
            },
        )
        with self.assertRaisesRegex(ValueError, 'outside its target inheritance tree'):
            candidate.validate()

    def test_incremental_delete_checks_owned_temporal_rows(self):
        candidate = IncrementalData(self.layout, self.backend)
        candidate.add(
            'tb_a_outstanding',
            {
                'id': 'a00001',
                'valid_from': '2026-01-01',
                'valid_to': '2026-02-01',
                'outstanding__state': 0,
                'outstanding': '10',
            },
        )
        candidate.remove('tb_a', candidate.select('tb_a', {'id': 'a00001'})[0])
        with self.assertRaisesRegex(ValueError, 'missing owner in tb_a_outstanding'):
            candidate.validate()
        clean = IncrementalData(self.layout, self.backend)
        clean.remove('tb_a', clean.select('tb_a', {'id': 'a00002'})[0])
        clean.validate()
        with Reader(clean.overlay, self.layout) as reader:
            reader.validate_candidate(clean)

        self.backend.close()
        with closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute("INSERT INTO tb_a_members VALUES ('a00003', '1')")
        backend = SQLiteBackend(self.database, self.layout)
        self.addCleanup(backend.close)
        candidate = IncrementalData(self.layout, backend)
        candidate.remove('tb_a', candidate.select('tb_a', {'id': 'a00003'})[0])
        with self.assertRaisesRegex(ValueError, 'missing owner in tb_a_members'):
            candidate.validate()

    def test_incremental_business_rejects_bad_type_and_unreadable_value(self):
        candidate = IncrementalData(self.layout, self.backend)
        row = candidate.select('tb_a', {'id': 'a00001'})[0]
        row['entity_type'] = 'Other'
        with Reader(candidate.overlay, self.layout) as reader:
            with self.assertRaisesRegex(ValueError, 'missing or invalid owner'):
                reader.validate_candidate(candidate)
        row['entity_type'] = 'A'
        row['text__state'] = 0
        row['text'] = 'changed'
        with Reader(candidate.overlay, self.layout) as reader:

            def unreadable(stored, field, physical):
                return NonNormalResult(Status.ERROR, 'decode failed')

            with self.assertRaisesRegex(ValueError, 'decode failed'):
                candidate.validate_business(reader._batch.implementation, unreadable)

    def test_candidate_batch_selection_and_non_key_filters(self):
        candidate = IncrementalData(self.layout, self.backend)
        row = candidate.select('tb_a', {'id': 'a00001'})[0]
        row['text__state'], row['text'] = 0, 'changed'
        self.assertEqual(candidate.overlay.rows('tb_a', {'text': 'changed'})[0]['id'], 'a00001')
        candidate.select('tb_a', {'entity_type': 'A'})
        self.assertEqual(candidate.select('tb_a', {'id': 'a00001'})[0]['text'], 'changed')
        for start, end in (('2026-01-01', '2026-02-01'), ('2026-02-01', '2026-03-01')):
            candidate.add(
                'tb_a_outstanding',
                {
                    'id': 'a00001',
                    'valid_from': start,
                    'valid_to': end,
                    'outstanding__state': 0,
                    'outstanding': '10',
                },
            )
        selections = [
            Selection((('id', 'a00001'),), 'scd', '2026-02-01', '2026-02-01'),
            Selection((('id', 'a00001'),)),
        ]
        result = candidate.overlay.rows_many('tb_a_outstanding', selections, ('valid_from',))
        self.assertEqual(result[0], [{'valid_from': '2026-02-01'}])
        self.assertEqual({row['valid_from'] for row in result[1]}, {'2026-01-01', '2026-02-01'})
        candidate.add(
            'tb_a_price',
            {
                'id': 'a00001',
                'date': '2026-02-01',
                'dim_source': 'One',
                'price__state': 0,
                'price': '10',
            },
        )
        result = candidate.overlay.rows_many(
            'tb_a_price', [Selection((('id', 'a00001'),), 'snapshot', '2026-02-01', '2026-02-01')]
        )
        self.assertEqual(result[0][0]['price'], '10')

    def test_incremental_business_validation_rejects_missing_owner(self):
        candidate = IncrementalData(self.layout, self.backend)
        candidate.add(
            'tb_a_outstanding',
            {
                'id': 'missing',
                'valid_from': '2026-01-01',
                'valid_to': '2026-02-01',
                'outstanding__state': 0,
                'outstanding': '10',
            },
        )
        candidate.validate()
        with Reader(candidate.overlay, self.layout) as reader:
            with self.assertRaisesRegex(ValueError, 'missing or invalid owner'):
                reader.validate_candidate(candidate)

    def test_insert_activates_reference_cycle_in_overlay(self):
        from tests.storage.test_validation import SOURCE as CYCLE_SOURCE

        self.write('cycle/yambl-root.lclcfg', "prefix: ''\n")
        self.write('cycle/main.yambl', CYCLE_SOURCE)
        layout = build_layout(compile_repository(self.root / 'cycle'))
        path = self.root / 'cycle.sqlite'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(ddl(layout))
            connection.execute(
                "INSERT INTO tb_node(id,entity_type,parent__state,parent) VALUES ('a','Node',0,'new')"
            )
        backend = SQLiteBackend(path, layout)
        self.addCleanup(backend.close)
        candidate = IncrementalData(layout, backend)
        entity = candidate.catalog.entity('Node')
        candidate.insert(entity, 'new')
        scope = (Condition(entity.id.name, 'new'),)
        candidate.put('Node.parent', scope, Image('Node.parent', ('value',), (('a',),)))
        candidate.validate()
        with Reader(candidate.overlay, layout) as reader:
            with self.assertRaisesRegex(ValueError, 'same-field reference cycle'):
                reader.validate_candidate(candidate)

    def test_sqlite_incremental_publication_and_precommit_failure(self):
        candidate = IncrementalData(self.layout, self.backend)
        plan = Plan(
            'update',
            self.layout.model.model_digest,
            (Group('g', (Operation('update', 'A', ('key', 'text'), (('a00001', 'new'),)),)),),
        )
        groups = Execution(candidate).apply(plan)
        with Reader(candidate.overlay, self.layout) as reader:
            reader.validate_candidate(candidate)
        transaction = seal(
            Transaction(
                '12345678-1234-1234-1234-123456789abc', '2026-10-26T00:00:00Z', plan, groups
            )
        )
        changes = candidate.overlay.changes()
        self.backend.close()

        original_connect = sqlite3.connect
        statements = []

        def trace_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite3, 'connect', side_effect=trace_connect):
            publish_sqlite(self.database, self.layout, changes, transaction)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute('SELECT text FROM tb_a WHERE id=?', ('a00001',)).fetchone(),
                ('new',),
            )
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tb_a').fetchone(), (2000,))
            self.assertEqual(
                connection.execute('SELECT id FROM tb_yambl_transactions').fetchone(),
                (transaction.id,),
            )
        deletes = [sql for sql in statements if sql.startswith('DELETE')]
        self.assertEqual(len(deletes), 1)
        self.assertIn('WHERE', deletes[0])

        with self.assertRaises(sqlite3.IntegrityError):
            publish_sqlite(self.database, self.layout, changes, transaction)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute('SELECT text FROM tb_a WHERE id=?', ('a00001',)).fetchone(),
                ('new',),
            )
            self.assertEqual(
                connection.execute('SELECT COUNT(*) FROM tb_yambl_transactions').fetchone(), (1,)
            )

    def test_sqlite_mid_publication_failure_restores_all_changed_tables(self):
        candidate = IncrementalData(self.layout, self.backend)
        scope = (Condition('key', 'a00001'),)
        plan = Plan(
            'update',
            self.layout.model.model_digest,
            (
                Group(
                    'g',
                    (
                        Operation('update', 'A', ('key', 'text'), (('a00001', 'new text'),)),
                        Operation(
                            'replace',
                            'A.price',
                            ('day', 'source', 'value'),
                            (('2026-01-01', 'One', 42),),
                            scope,
                        ),
                    ),
                ),
            ),
        )
        groups = Execution(candidate).apply(plan)
        with Reader(candidate.overlay, self.layout) as reader:
            reader.validate_candidate(candidate)
        transaction = seal(
            Transaction(
                '12345678-1234-1234-1234-123456789abc', '2026-10-26T00:00:00Z', plan, groups
            )
        )
        changes = candidate.overlay.changes()
        self.assertIn('tb_a', changes)
        self.assertIn('tb_a_price', changes)
        self.backend.close()
        original_connect = sqlite3.connect

        class FailOnPrice(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith('INSERT INTO "tb_a_price"'):
                    raise sqlite3.OperationalError('injected second table failure')
                return super().execute(sql, *args)

        def connect(*args, **kwargs):
            return original_connect(*args, factory=FailOnPrice, **kwargs)

        with patch.object(sqlite3, 'connect', side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'second table failure'):
                publish_sqlite(self.database, self.layout, changes, transaction)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute('SELECT text FROM tb_a WHERE id=?', ('a00001',)).fetchone(),
                (None,),
            )
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tb_a_price').fetchone(), (0,))
            self.assertEqual(
                connection.execute('SELECT COUNT(*) FROM tb_yambl_transactions').fetchone(), (0,)
            )

    def test_sqlite_readers_keep_snapshots_across_incremental_apply_and_rollback(self):
        old = SQLiteBackend(self.database, self.layout)
        self.addCleanup(old.close)
        candidate = IncrementalData(self.layout, self.backend)
        plan = Plan(
            'update',
            self.layout.model.model_digest,
            (Group('g', (Operation('update', 'A', ('key', 'text'), (('a00001', 'new'),)),)),),
        )
        groups = Execution(candidate).apply(plan)
        transaction = seal(
            Transaction(
                '12345678-1234-1234-1234-123456789abc', '2026-10-26T00:00:00Z', plan, groups
            )
        )
        changes = candidate.overlay.changes()
        self.backend.close()
        publish_sqlite(self.database, self.layout, changes, transaction)
        self.assertIsNone(old.rows('tb_a', {'id': 'a00001'})[0]['text'])

        after_apply = SQLiteBackend(self.database, self.layout)
        self.addCleanup(after_apply.close)
        source = SQLiteBackend(self.database, self.layout)
        candidate = IncrementalData(self.layout, source)
        reverse_groups = prepare_rollback(candidate, transaction)
        candidate.validate()
        reverse = seal(
            Transaction(
                '12345678-1234-1234-1234-123456789abd',
                '2026-10-26T00:00:01Z',
                plan,
                reverse_groups,
                transaction.id,
            )
        )
        changes = candidate.overlay.changes()
        source.close()
        publish_sqlite(self.database, self.layout, changes, reverse)
        self.assertEqual(after_apply.rows('tb_a', {'id': 'a00001'})[0]['text'], 'new')
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute('SELECT text FROM tb_a WHERE id=?', ('a00001',)).fetchone(),
                (None,),
            )

    def test_file_generation_reuses_unaffected_files_and_preserves_old_snapshot(self):
        baseline = {table.name: self.backend.rows(table.name, {}) for table in self.layout.tables}
        source_path = self.root / 'old-generation'
        for name, contents in repository_contents(self.layout, baseline).items():
            path = source_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding='utf-8', newline='\n')
        source = FileBackend(source_path, self.layout)
        self.addCleanup(source.close)
        old = FileBackend(source_path, self.layout)
        self.addCleanup(old.close)
        candidate = IncrementalData(self.layout, source)
        plan = Plan(
            'change',
            self.layout.model.model_digest,
            (
                Group(
                    'g', (Operation('update', 'A', ('key', 'text'), (('a00001', '变长 value'),)),)
                ),
            ),
        )
        Execution(candidate).apply(plan)
        with Reader(candidate.overlay, self.layout) as reader:
            reader.validate_candidate(candidate)
        probe = self.root / 'link-probe'
        try:
            os.link(source_path / 'tables' / 'tb_a_outstanding.jsonl', probe)
        except OSError:
            hardlinks_supported = False
        else:
            hardlinks_supported = True
            probe.unlink()
        destination = self.root / 'new-generation'
        if not hardlinks_supported:
            with self.assertRaisesRegex(OSError, 'file backend requires hard links'):
                prepare_files(source, destination, self.layout, candidate.overlay.changes())
            self.assertIsNone(old.rows('tb_a', {'id': 'a00001'})[0]['text'])
            return
        prepare_files(source, destination, self.layout, candidate.overlay.changes())
        current = FileBackend(destination, self.layout)
        self.addCleanup(current.close)
        self.assertIsNone(old.rows('tb_a', {'id': 'a00001'})[0]['text'])
        self.assertEqual(current.rows('tb_a', {'id': 'a00001'})[0]['text'], '变长 value')
        self.assertIsNone(current.rows('tb_a', {'id': 'a01999'})[0]['text'])
        self.assertEqual(len(current.rows('tb_a', {})), 2000)
        for category in ('tables', 'indexes'):
            self.assertEqual(
                os.path.samefile(
                    source_path / category / 'tb_a_outstanding.jsonl',
                    destination / category / 'tb_a_outstanding.jsonl',
                ),
                hardlinks_supported,
            )
            self.assertFalse(
                os.path.samefile(
                    source_path / category / 'tb_a.jsonl',
                    destination / category / 'tb_a.jsonl',
                )
            )
        changed = candidate.overlay.changes()['tb_a'][0]
        assert changed.before is not None
        incorrect = RowChange(changed.key, {**changed.before, 'text': 'wrong'}, changed.after)
        with self.assertRaisesRegex(ValueError, 'original row changed'):
            prepare_files(
                source, self.root / 'rejected-generation', self.layout, {'tb_a': (incorrect,)}
            )
        self.assertIsNone(old.rows('tb_a', {'id': 'a00001'})[0]['text'])
        with self.assertRaisesRegex(ValueError, 'duplicate changed primary key'):
            prepare_files(
                source,
                self.root / 'duplicate-generation',
                self.layout,
                {'tb_a': (changed, changed)},
            )
        absent = RowChange(('absent',), changed.before, None)
        with self.assertRaisesRegex(ValueError, 'original row missing'):
            prepare_files(
                source, self.root / 'missing-generation', self.layout, {'tb_a': (absent,)}
            )
        with self.assertRaisesRegex(ValueError, 'unknown changed table'):
            prepare_files(source, self.root / 'unknown-generation', self.layout, {'unknown': ()})
        with patch(
            'yambl_compiler.transactions.incremental_publication.os.link',
            side_effect=OSError('unsupported'),
        ):
            with self.assertRaisesRegex(OSError, 'file backend requires hard links'):
                prepare_files(source, self.root / 'unsupported-generation', self.layout, {})
        self.assertIsNone(old.rows('tb_a', {'id': 'a00001'})[0]['text'])

    def test_legacy_file_generation_gets_complete_new_indexes(self):
        baseline = {table.name: self.backend.rows(table.name, {}) for table in self.layout.tables}
        source_path = self.root / 'legacy-generation'
        for name, contents in repository_contents(self.layout, baseline).items():
            if name.startswith('indexes/'):
                continue
            path = source_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == 'repository.json':
                metadata = json.loads(contents)
                del metadata['indexes']
                contents = json.dumps(metadata)
            path.write_text(contents, encoding='utf-8', newline='\n')
        source = FileBackend(source_path, self.layout)
        self.addCleanup(source.close)
        destination = self.root / 'indexed-generation'
        probe = self.root / 'legacy-link-probe'
        try:
            os.link(source_path / 'tables' / 'tb_a.jsonl', probe)
        except OSError:
            with self.assertRaisesRegex(OSError, 'file backend requires hard links'):
                prepare_files(source, destination, self.layout, {})
            return
        probe.unlink()
        prepare_files(source, destination, self.layout, {})
        current = FileBackend(destination, self.layout)
        self.addCleanup(current.close)
        self.assertEqual(set(current.index_hashes), {table.name for table in self.layout.tables})
        self.assertEqual(current.rows('tb_a', {'id': 'a01999'})[0]['id'], 'a01999')
        self.assertTrue(
            os.path.samefile(
                source_path / 'tables' / 'tb_a.jsonl', destination / 'tables' / 'tb_a.jsonl'
            )
        )
