"""Exercise integer state storage through generated schemas and transactions."""

import asyncio
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import date

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.records import quote
from yambl_compiler.storage.results import NonNormalResult, Status, Success
from yambl_compiler.transactions.writing import dumps

SOURCE = """namespace core
decodes Sources { - 1 One: 'One' }
entity Tag { id key: string }
entity Item {
    id key: string
    static attr plain: string
    @sparse('rare') static attr rare: string
    @dense('group') static attr dense: string
    static attr tags: collection<Tag>
    config attr setting: string
    @temporal(day: date) snapshot attr snap: string
    @temporal(day: date) scd attr period: string
}
entity Child inherits Item {}
"""


class StateEncodingTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        self.layout = generate(
            compile_repository(self.root), self.root / 'generated', 'sample_model'
        )
        self.model = load_generated(self.root / 'generated' / 'sample_model', 'sample_model')

    def plan(self):
        states = ('VALUE', 'NO_ENTRY', 'EMPTY', 'NOT_APPLICABLE')
        records = []
        operations = [Operation('insert', 'core.Tag', ('key',), (('tag',),))]
        for name in states:
            value = 'text' if name == 'VALUE' else Symbol(name)
            tags = ('tag',) if name == 'VALUE' else Symbol(name)
            records.append((name, value, value, value, tags))
        operations.append(
            Operation(
                'insert', 'core.Item', ('key', 'plain', 'rare', 'dense', 'tags'), tuple(records)
            )
        )
        for name in states:
            value = 'text' if name == 'VALUE' else Symbol(name)
            scope = (Condition('key', name),)
            operations.extend(
                (
                    Operation('replace', 'core.Item.setting', ('value',), ((value,),), scope),
                    Operation(
                        'replace',
                        'core.Item.snap',
                        ('day', 'value'),
                        (('2026-10-29', value),),
                        scope,
                    ),
                    Operation(
                        'replace',
                        'core.Item.period',
                        ('valid_from', 'valid_to', 'value'),
                        (('2026-10-01', '2026-11-01', value),),
                        scope,
                    ),
                )
            )
        plan = Plan('states', self.layout.model.model_digest, (Group('states', tuple(operations)),))
        return self.write('states.yaplan', dumps(plan))

    def test_integer_states_round_trip_and_rollback_both_backends(self):
        plan = self.plan()
        expected = {'VALUE': 0, 'NO_ENTRY': 1, 'EMPTY': 2, 'NOT_APPLICABLE': 3}
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with asyncio.run(self.model.SampleData.create(config)) as store:
                    before = store.snapshot().tables
                    applied = store.apply(plan)
                    self.assertEqual(applied.status, 'committed')
                    tables = store.snapshot().tables
                    for name, code in expected.items():
                        for field in (
                            'plain',
                            'rare',
                            'dense',
                            'tags',
                            'setting',
                            'snap',
                            'period',
                        ):
                            stored = self.layout.fields['core.Item.' + field]
                            rows = [row for row in tables[stored.table] if row['id'] == name]
                            if stored.key_values:
                                rows = [
                                    row
                                    for row in rows
                                    if all(row[k] == v for k, v in stored.key_values)
                                ]
                            self.assertEqual(len(rows), 1, (name, field))
                            self.assertEqual(rows[0][stored.state], code, (name, field))
                            self.assertIs(type(rows[0][stored.state]), int)
                        reader = store.read()
                        try:
                            item = reader.get_item(name).value
                            values = [item.plain, item.rare, item.dense, item.setting]
                            values.extend(
                                (
                                    item.read_snap(date(2026, 10, 29)),
                                    item.read_period(date(2026, 10, 29)),
                                )
                            )
                            for value in values:
                                self.assertEqual(
                                    value,
                                    (
                                        Success('text')
                                        if name == 'VALUE'
                                        else NonNormalResult(Status(name))
                                    ),
                                )
                            if name == 'VALUE':
                                self.assertEqual({member.id for member in item.tags.value}, {'tag'})
                            else:
                                self.assertEqual(item.tags.status, Status(name))
                        finally:
                            reader.close()
                    reverted = store.rollback(applied.path)
                    self.assertEqual(reverted.status, 'committed')
                    self.assertEqual(store.snapshot().tables, before)
                    redone = store.rollback(reverted.path)
                    self.assertEqual(redone.status, 'committed')
                    self.assertEqual(store.snapshot().tables, tables)

    def test_sqlite_state_columns_have_integer_defaults_and_checks(self):
        config = self.write('schema.lclcfg', "backend: 'sqlite'\npath: 'schema.data'\n")
        with asyncio.run(self.model.SampleData.create(config)) as store:
            store.apply(self.plan())
        with closing(sqlite3.connect(self.root / 'schema.data')) as connection:
            connection.execute("INSERT INTO tb_hierarchy(hierarchy_id) VALUES ('test')")
            connection.execute("INSERT INTO tb_config_group(config_group_id) VALUES ('test')")
            states = {(field.table, field.state) for field in self.layout.fields.values()}
            for table, column in sorted(states):
                with self.subTest(table=table, column=column):
                    columns = {
                        row[1]: row
                        for row in connection.execute(f'PRAGMA table_info({quote(table)})')
                    }
                    self.assertEqual(columns[column][2].upper(), 'INTEGER')
                    self.assertEqual(columns[column][4], '1')
                    self.assertGreater(
                        connection.execute(f'SELECT COUNT(*) FROM {quote(table)}').fetchone()[0], 0
                    )
                    for invalid in (-1, 4, 'VALUE'):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                f'UPDATE {quote(table)} SET {quote(column)} = ?', (invalid,)
                            )
            self.assertEqual(
                connection.execute(
                    "SELECT hierarchy_entries__state FROM tb_hierarchy WHERE hierarchy_id='test'"
                ).fetchone(),
                (1,),
            )
            stored = self.layout.fields['core.Item.plain']
            for identity, state in (('EMPTY', 0), ('VALUE', 2)):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        f'UPDATE {quote(stored.table)} SET {quote(stored.state)} = ? WHERE id = ?',
                        (state, identity),
                    )

    def test_jsonl_rejects_invalid_state_codes_and_types(self):
        config = self.write('seed.lclcfg', "backend: 'files'\npath: 'seed.data'\n")
        with asyncio.run(self.model.SampleData.create(config)) as store:
            store.apply(self.plan())
            original = store.snapshot().tables
        stored = self.layout.fields['core.Item.plain']
        cases = [('VALUE', invalid) for invalid in (-1, 4, 'VALUE', '0', True, 0.0, 2)]
        cases.append(('EMPTY', 0))
        for index, (identity, invalid) in enumerate(cases):
            with self.subTest(identity=identity, invalid=invalid):
                tables = deepcopy(original)
                row = next(row for row in tables[stored.table] if row['id'] == identity)
                row[stored.state] = invalid
                root = self.root / f'invalid-{index}'
                for path, contents in repository_contents(self.layout, tables).items():
                    destination = root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(contents, encoding='utf-8', newline='\n')
                opened = self.model.SampleReader.open_files(root)
                self.assertIsInstance(opened, Success)
                try:
                    result = opened.value.get_item(identity)
                    self.assertEqual(result.status, Status.ERROR)
                    self.assertIn('invalid', result.message)
                finally:
                    opened.value.close()
