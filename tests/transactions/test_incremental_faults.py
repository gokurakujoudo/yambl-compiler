"""Verify incremental publication failures through real generated data stores."""

import asyncio
import json
import sqlite3
from unittest.mock import patch

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.shared.model import Condition
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.languages.yatran.model import Image
from yambl_compiler.storage.backends import FileBackend, repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import NonNormalResult, Status, Success
from yambl_compiler.transactions import incremental_publication as publication
from yambl_compiler.transactions import repository
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution
from yambl_compiler.transactions.overlay import RowChange
from yambl_compiler.transactions.writing import dumps


class IncrementalFaultTests(CatalogCase):
    """Exercise commit and rollback outcomes without touching existing repositories."""

    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.module = load_generated(output / 'sample_model', 'sample_model')
        self.seed = self.plan('seed', Operation('insert', 'A', ('key', 'text'), (('a', 'before'),)))
        self.update = self.plan(
            'update', Operation('update', 'A', ('key', 'text'), (('a', 'after'),))
        )

    def plan(self, name, operation):
        """Write one model-bound test Plan."""
        document = Plan(name, self.catalog.layout.model.model_digest, (Group('g', (operation,)),))
        return self.write(name + '.yaplan', dumps(document))

    def store(self, backend):
        """Create an isolated repository and commit a baseline record."""
        config = self.write(backend + '.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n")
        data = asyncio.run(self.module.SampleData.create(config))
        self.addCleanup(data.close)
        transaction = data.apply(self.seed)
        self.assertEqual(transaction.status, 'committed')
        return data, config, transaction

    def read_text(self, data):
        """Return the test record from a fresh reader snapshot."""
        with data.read() as reader:
            entity = reader.get_a('a')
            self.assertIsInstance(entity, Success)
            value = entity.value.read_text()
            self.assertIsInstance(value, Success)
            return value.value

    def test_files_hardlink_failure_preserves_apply_and_rollback(self):
        data, config, seed = self.store('files')
        pointer = (data.path / 'repository.json').read_bytes()
        before = data.snapshot().tables
        exports = set(data.transaction_directory().glob('*.yatran'))
        unknown = data.path / 'versions' / 'user-file'
        unknown.write_text('preserve', encoding='utf-8')
        old = data.read()
        self.addCleanup(old.close)
        with patch.object(publication.os, 'link', side_effect=OSError('unsupported')):
            for action in (lambda: data.apply(self.update), lambda: data.rollback(seed.path)):
                with self.assertRaisesRegex(OSError, 'file backend requires hard links'):
                    action()
                self.assertEqual((data.path / 'repository.json').read_bytes(), pointer)
                self.assertEqual(data.snapshot().tables, before)
                self.assertEqual(set(data.transaction_directory().glob('*.yatran')), exports)
                self.assertEqual(old.get_a('a').value.read_text().value, 'before')
        reopened = asyncio.run(self.module.SampleData.open(config))
        self.addCleanup(reopened.close)
        self.assertEqual(self.read_text(reopened), 'before')
        self.assertEqual(unknown.read_text(encoding='utf-8'), 'preserve')
        self.assertEqual((data.path / 'repository.json').read_bytes(), pointer)

    def test_source_mismatch_aborts_both_backends_without_publishing(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend):
                data, config, seed = self.store(backend)
                before = data.snapshot().tables
                exports = set(data.transaction_directory().glob('*.yatran'))
                name = 'prepare_files' if backend == 'files' else 'publish_sqlite'
                original = getattr(repository, name)

                def mismatched(source, destination, layout, changes, transaction):
                    changed = dict(changes)
                    original_row = changed['tb_a'][0]
                    before_row = dict(original_row.before)
                    before_row['text'] = 'not the source image'
                    changed['tb_a'] = (RowChange(original_row.key, before_row, original_row.after),)
                    return original(source, destination, layout, changed, transaction)

                def missing(path, layout, changes, transaction):
                    changed = dict(changes)
                    original_row = changed['tb_a'][0]
                    changed['tb_a'] = (
                        RowChange(('absent',), original_row.before, original_row.after),
                    )
                    return original(path, layout, changed, transaction)

                with patch.object(repository, name, mismatched if backend == 'files' else missing):
                    with self.assertRaisesRegex(ValueError, 'original row (changed|missing)'):
                        data.apply(self.update)
                self.assertEqual(data.snapshot().tables, before)
                self.assertEqual(set(data.transaction_directory().glob('*.yatran')), exports)
                reopened = asyncio.run(self.module.SampleData.open(config))
                self.addCleanup(reopened.close)
                self.assertEqual(self.read_text(reopened), 'before')
                self.assertEqual(seed.path.read_bytes(), next(iter(exports)).read_bytes())

    def test_sqlite_commit_failure_and_close_failure_preserve_original_error(self):
        data, _, seed = self.store('sqlite')
        before = data.snapshot().tables
        exports = set(data.transaction_directory().glob('*.yatran'))
        original_connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError('commit rejected')

            def close(self):
                super().close()
                raise sqlite3.OperationalError('close rejected')

        def connect(path, *args, **kwargs):
            if path == data.path:
                return original_connect(path, *args, **kwargs, factory=FailingConnection)
            return original_connect(path, *args, **kwargs)

        original_publish = repository.publish_sqlite

        def publish(*args, **kwargs):
            with patch.object(publication.sqlite3, 'connect', side_effect=connect):
                return original_publish(*args, **kwargs)

        with patch.object(repository, 'publish_sqlite', side_effect=publish):
            for action in (lambda: data.apply(self.update), lambda: data.rollback(seed.path)):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'commit rejected') as failure:
                    action()
                self.assertTrue(
                    any('close rejected' in note for note in failure.exception.__notes__)
                )
                self.assertEqual(data.snapshot().tables, before)
                self.assertEqual(set(data.transaction_directory().glob('*.yatran')), exports)

    def test_sqlite_close_after_commit_keeps_committed_report(self):
        data, config, _ = self.store('sqlite')
        original_connect = sqlite3.connect

        class ClosingFailure(sqlite3.Connection):
            def close(self):
                super().close()
                raise sqlite3.OperationalError('close failed after commit')

        def connect(path, *args, **kwargs):
            if path == data.path:
                return original_connect(path, *args, **kwargs, factory=ClosingFailure)
            return original_connect(path, *args, **kwargs)

        original_publish = repository.publish_sqlite

        def publish(*args, **kwargs):
            with patch.object(publication.sqlite3, 'connect', side_effect=connect):
                return original_publish(*args, **kwargs)

        with patch.object(repository, 'publish_sqlite', side_effect=publish):
            with self.assertLogs(publication.LOGGER, level='WARNING') as logs:
                report = data.apply(self.update)
        self.assertEqual(report.status, 'committed')
        self.assertTrue(report.path.is_file())
        self.assertIn('SQLite connection close failed after commit', '\n'.join(logs.output))
        reopened = asyncio.run(self.module.SampleData.open(config))
        self.addCleanup(reopened.close)
        self.assertEqual(self.read_text(reopened), 'after')
        self.assertEqual(reopened.rollback(report.path).status, 'committed')
        self.assertEqual(self.read_text(reopened), 'before')

    def test_damaged_file_sources_fail_before_publication(self):
        store, _, _ = self.store('files')
        tables = store.snapshot().tables
        expected = repository_contents(self.catalog.layout, tables)
        pointer = (store.path / 'repository.json').read_bytes()
        exports = set(store.transaction_directory().glob('*.yatran'))
        for legacy in (False, True):
            for malformed in (False, True):
                with self.subTest(legacy=legacy, malformed=malformed):
                    label = f'{legacy}-{malformed}'
                    source_path = self.root / ('source-' + label)
                    for name, content in expected.items():
                        path = source_path / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding='utf-8', newline='\n')
                    metadata = source_path / 'repository.json'
                    if legacy:
                        document = json.loads(metadata.read_text(encoding='utf-8'))
                        del document['indexes']
                        metadata.write_text(json.dumps(document), encoding='utf-8')
                    table_path = source_path / 'tables' / 'tb_a.jsonl'
                    if malformed:
                        table_path.write_text('[]\n', encoding='utf-8')
                    else:
                        row = json.loads(table_path.read_text(encoding='utf-8'))
                        row['id'] = 'different'
                        table_path.write_text(json.dumps(row) + '\n', encoding='utf-8')
                    source = FileBackend(source_path, self.catalog.layout)
                    self.addCleanup(source.close)
                    destination = self.root / ('destination-' + label)
                    (destination / 'tables').mkdir(parents=True)
                    (destination / 'indexes').mkdir()
                    table = next(t for t in self.catalog.layout.tables if t.name == 'tb_a')
                    expected_error = (
                        'invalid source row' if malformed else 'source table digest mismatch'
                    )
                    with self.assertRaisesRegex(ValueError, expected_error):
                        if legacy:
                            publication._index_legacy(source, destination, table)
                        else:
                            publication._stream_table(source, destination, table, ())
        self.assertEqual((store.path / 'repository.json').read_bytes(), pointer)
        self.assertEqual(set(store.transaction_directory().glob('*.yatran')), exports)
        self.assertEqual(self.read_text(store), 'before')

    def test_empty_pending_change_is_a_noop_and_unknown_table_is_rejected(self):
        files, _, _ = self.store('files')
        source = FileBackend(files.file_version(), self.catalog.layout)
        self.addCleanup(source.close)
        destination = self.root / 'noop-generation'
        (destination / 'tables').mkdir(parents=True)
        (destination / 'indexes').mkdir()
        table = next(t for t in self.catalog.layout.tables if t.name == 'tb_a')
        unchanged = publication._stream_table(
            source, destination, table, (RowChange(('absent',), None, None),)
        )
        self.assertEqual(unchanged[0], source.hashes['tb_a'])
        self.assertEqual(self.read_text(files), 'before')
        sqlite, _, seed = self.store('sqlite')
        before = sqlite.snapshot().tables
        with self.assertRaisesRegex(ValueError, 'unknown changed table'):
            publication.publish_sqlite(sqlite.path, self.catalog.layout, {'unknown': ()}, None)
        self.assertEqual(sqlite.snapshot().tables, before)
        self.assertTrue(seed.path.is_file())

    def test_legacy_repository_without_indexes_remains_readable(self):
        store, _, _ = self.store('files')
        source = self.root / 'legacy-unindexed'
        contents = repository_contents(self.catalog.layout, store.snapshot().tables)
        metadata = json.loads(contents['repository.json'])
        del metadata['indexes']
        contents['repository.json'] = json.dumps(metadata) + '\n'
        for name, content in contents.items():
            if name.startswith('indexes/'):
                continue
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        backend = FileBackend(source, self.catalog.layout)
        self.addCleanup(backend.close)
        self.assertEqual(backend.index_hashes, {})
        self.assertIsNone(backend.validate_indexes())
        self.assertEqual(backend.rows('tb_a', {'id': 'a'})[0]['text'], 'before')

    def test_expectation_validation_keeps_data_unchanged_on_success_and_failure(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend):
                store, _, _ = self.store(backend)
                before = store.snapshot().tables
                present = self.plan(
                    'present',
                    Operation('delete', 'A.text', (), (), (Condition('key', 'a'),), expect='one'),
                )
                absent = self.plan(
                    'absent',
                    Operation('delete', 'A.price', (), (), (Condition('key', 'a'),), expect='one'),
                )
                self.assertEqual(store.validate_plan_expectations(present).name, 'present')
                with self.assertRaisesRegex(ValueError, 'expect one matched 0'):
                    store.validate_plan_expectations(absent)
                self.assertEqual(store.snapshot().tables, before)

    def test_expectation_timing_and_close_failures_preserve_error_context(self):
        store, _, _ = self.store('files')
        absent = self.plan(
            'absent', Operation('delete', 'A.price', (), (), (Condition('key', 'a'),), expect='one')
        )
        original_open = store.open_backend

        def open_with_failed_close():
            backend = original_open()
            original_close = backend.close

            def close():
                original_close()
                raise OSError('reader close failure')

            backend.close = close
            return backend

        with patch.object(store, 'open_backend', side_effect=open_with_failed_close):
            for source in (self.update, absent):
                with self.subTest(source=source.name):
                    with self.assertLogs(repository.LOGGER, level='INFO') as logs:
                        with self.assertRaisesRegex(OSError, 'reader close failure') as failure:
                            store.validate_plan_expectations(source)
                    self.assertIn(
                        'operation=validate_plan phase=expectations', '\n'.join(logs.output)
                    )
                    if source == absent:
                        self.assertIsInstance(failure.exception.__context__, ValueError)
                        self.assertIn('expect one matched 0', str(failure.exception.__context__))
                    else:
                        self.assertIsNone(failure.exception.__context__)

    def test_detached_candidate_commit_and_rollback_for_each_backend(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend):
                store, _, _ = self.store(backend)
                data = store.snapshot()
                plan = parse(self.update.read_text(encoding='utf-8'))
                groups = Execution(data).prepare(plan)
                report = store.finish(data, plan, groups)
                self.assertEqual(report.status, 'committed')
                self.assertTrue(report.path.is_file())
                self.assertEqual(self.read_text(store), 'after')
                self.assertEqual(store.rollback(report.path).status, 'committed')
                self.assertEqual(self.read_text(store), 'before')

    def test_detached_candidate_validation_failures_preserve_both_backends(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend):
                store, _, _ = self.store(backend)
                plan = parse(self.update.read_text(encoding='utf-8'))
                before = store.snapshot().tables
                exports = set(store.transaction_directory().glob('*.yatran'))
                invalid = store.snapshot()
                row = invalid.select('tb_a', {'id': 'a'})[0]
                row['count__state'], row['count'] = 'VALUE', '01'
                with self.assertRaisesRegex(ValueError, 'candidate data validation failed'):
                    store.finish(invalid, plan, ())
                clean = store.snapshot()
                with patch.object(
                    store.reader_type, 'validate', return_value=NonNormalResult(Status.ERROR, 'io')
                ):
                    with self.assertRaisesRegex(ValueError, 'candidate data validation failed'):
                        store.finish(clean, plan, ())
                self.assertEqual(store.snapshot().tables, before)
                self.assertEqual(set(store.transaction_directory().glob('*.yatran')), exports)

    def test_file_publisher_rejects_a_sqlite_candidate(self):
        files, _, _ = self.store('files')
        sqlite, _, _ = self.store('sqlite')
        before = files.snapshot().tables
        with sqlite.mutable_candidate('test') as candidate:
            with self.assertRaisesRegex(ValueError, 'file publication requires a file snapshot'):
                files.publish_incremental_files(candidate, None)
        self.assertEqual(files.snapshot().tables, before)

    def test_candidate_close_failure_retains_precommit_error_or_committed_report(self):
        store, config, _ = self.store('files')
        original_open = store.open_backend

        def open_with_failed_close():
            backend = original_open()
            original_close = backend.close

            def close():
                original_close()
                raise OSError('candidate close failed')

            backend.close = close
            return backend

        missing = self.plan(
            'missing', Operation('update', 'A', ('key', 'text'), (('missing', 'bad'),))
        )
        with patch.object(store, 'open_backend', side_effect=open_with_failed_close):
            with self.assertRaisesRegex(ValueError, 'entity does not exist') as failure:
                store.apply(missing)
            self.assertIn('candidate close failed', '\n'.join(failure.exception.__notes__))
            with self.assertLogs(repository.LOGGER, level='WARNING') as logs:
                report = store.apply(self.update)
        self.assertEqual(report.status, 'committed')
        self.assertTrue(report.path.is_file())
        self.assertIn('committed candidate snapshot close failed', '\n'.join(logs.output))
        reopened = asyncio.run(self.module.SampleData.open(config))
        self.addCleanup(reopened.close)
        self.assertEqual(self.read_text(reopened), 'after')


class DetachedCandidateValidationTests(CatalogCase):
    """Exercise detached candidate defenses independently from Plan validation."""

    def candidate(self):
        tables = {
            table.name: [
                dict(zip((column.name for column in table.columns), row, strict=True))
                for row in table.rows
            ]
            for table in self.catalog.layout.tables
        }
        data = Data(self.catalog.layout, tables)
        data.insert(data.catalog.entity('A'), 'a')
        return data

    def test_replacement_rows_cannot_escape_scope(self):
        data = self.candidate()
        data.insert(data.catalog.entity('A'), 'b')
        scope = (Condition('key', 'a'),)
        with self.assertRaisesRegex(ValueError, 'replacement record is outside scope'):
            data.put('A.text', scope, Image('A.text', ('key', 'value'), (('b', 'wrong'),)))
        with self.assertRaisesRegex(ValueError, 'replacement interval crosses scope boundary'):
            data.put(
                'A.outstanding',
                (*scope, Condition('day', '2026-01-01', '2026-02-01')),
                Image(
                    'A.outstanding',
                    ('valid_from', 'valid_to', 'value'),
                    (('2026-01-01', '2026-02-02', 1),),
                ),
            )

    def test_detached_validation_checks_overlaps_and_config_groups(self):
        data = self.candidate()
        data.tables['tb_a_outstanding'].extend(
            [
                {
                    'id': 'a',
                    'valid_from': '2026-01-01',
                    'valid_to': '2026-02-01',
                    'outstanding__state': 'VALUE',
                    'outstanding': '1',
                },
                {
                    'id': 'a',
                    'valid_from': '2026-01-31',
                    'valid_to': '2026-03-01',
                    'outstanding__state': 'VALUE',
                    'outstanding': '2',
                },
            ]
        )
        with self.assertRaisesRegex(ValueError, 'overlapping SCD intervals'):
            data.validate()
        data.tables['tb_a_outstanding'].clear()
        group = data.blank('tb_config_group', {'config_group_id': 'group'})
        data.tables['tb_config_group'].append(group)
        with self.assertRaisesRegex(ValueError, 'ConfigGroup requires'):
            data.validate()
        group.update(
            {
                'target_entity_type__state': 'VALUE',
                'target_entity_type': 'Missing',
                'priority__state': 'VALUE',
                'priority': '1',
                'when__state': 'VALUE',
                'then_set__state': 'VALUE',
            }
        )
        with self.assertRaisesRegex(ValueError, 'target must be a business entity'):
            data.validate()
        group['target_entity_type'] = 'A'
        data.tables['tb_config_group_when'].append(
            {'config_group_id': 'group', 'field': 'Other.label', 'value': '{"type":{},"value":"x"}'}
        )
        with self.assertRaisesRegex(ValueError, 'outside its target inheritance tree'):
            data.validate()
