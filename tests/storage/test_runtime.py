"""Exercise generated handles against real SQLite files."""

import importlib.util
import sqlite3
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.layout import quote, table_name
from yambl_compiler.storage.results import EntityRef, Status, Success
from yambl_compiler.storage.runtime import EntityHandle

SOURCE = """
decodes Currencies { - 1 USD: 'US dollar' - 2 CNY: '人民币' }
entity A {
    id key: string
    attr text: string
    attr flag: bool
    attr count: integer
    attr day: date
    attr amount: decimal[6, 2]
    attr currency: code<Currencies>
    attr other: Other
    attr members: collection<Other>
    config attr limit: integer = 20
    config attr enabled: bool = true
    config attr threshold: decimal
    config attr peer: Other
    config attr peers: collection<Other>
    @dimension(source: code<'Sources'>)
    @temporal(day: date)
    snapshot attr price: decimal
    @temporal(day: date)
    snapshot attr positions: collection<Other>
    @temporal(day: date)
    scd attr outstanding: decimal
    @temporal(day: date)
    scd attr owners: collection<Other>
}
entity B inherits A { attr extra: string }
entity C inherits A { @override config attr limit: integer = 30 }
entity Other { id number: integer attr label: string }
"""


class RuntimeTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', SOURCE)
        self.model = compile_repository(self.root)
        self.output = self.root / 'generated'
        self.layout = generate(self.model, self.output, 'sample_model')
        spec = importlib.util.spec_from_file_location(
            'sample_model', self.output / 'sample_model/__init__.py'
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.path = self.root / 'example.db'
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.executescript((self.output / 'schema.sql').read_text(encoding='utf-8'))
        for root, values in [
            ('A', [('a', 'A'), ('b', 'B'), ('c', 'C')]),
            ('Other', [('1', 'Other')]),
        ]:
            self.writer.executemany(
                f'INSERT INTO {quote(table_name(root))} (id, entity_type) VALUES (?, ?)', values
            )
        self.writer.commit()

    def open(self):
        opened = self.module.SampleReader.open(self.path)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        self.addCleanup(reader.close)
        return reader

    def put(self, identity, value=None, state='VALUE', id='a', **keys):
        stored = self.layout.fields[identity]
        values = {'id': id, **dict(stored.key_values), **keys, stored.state: state}
        if stored.value:
            values[stored.value] = value
        if stored.table == table_name(stored.root):
            values['entity_type'] = self.writer.execute(
                f'SELECT entity_type FROM {quote(stored.table)} WHERE id = ?', (id,)
            ).fetchone()[0]
        cols = ', '.join(quote(k) for k in values)
        self.writer.execute(
            f'INSERT INTO {quote(stored.table)} ({cols}) VALUES ({", ".join("?" for _ in values)}) ON CONFLICT DO UPDATE SET '
            + ', '.join(quote(k) + ' = excluded.' + quote(k) for k in values),
            tuple(values.values()),
        )
        self.writer.commit()

    def test_handles_states_defaults_and_references(self):
        self.put('A.text', 'hello')
        self.put('A.flag', 1)
        self.put('A.count', '123456789012345678901234567890')
        self.put('A.day', '2026-10-24')
        self.put('A.amount', '1234.50')
        self.put('A.currency', 'USD')
        self.put('A.other', '99')
        self.put('A.members')
        reader = self.open()
        a = reader.get_a('a').value
        self.assertEqual(a.read_text(), Success('hello'))
        self.assertEqual(a.read_flag(), Success(True))
        self.assertEqual(a.read_count(), Success(123456789012345678901234567890))
        self.assertEqual(a.read_day(), Success(date(2026, 10, 24)))
        self.assertEqual(a.read_amount(), Success(Decimal('1234.50')))
        self.assertIs(a.read_currency().value, self.module.CurrenciesCode.USD)
        self.assertEqual(a.read_members(), Success(frozenset()))
        self.assertEqual(a.read_other().value, EntityRef('Other', 99))
        self.assertEqual(reader.resolve(a.read_other().value).status, Status.ENTITY_NOT_FOUND)
        self.assertEqual(a.read_limit(), Success(20, 'model_default'))
        self.assertEqual(a.read_enabled(), Success(True, 'model_default'))
        self.assertEqual(reader.get_c('c').value.read_limit(), Success(30, 'model_default'))
        self.assertEqual(a.read_extra().status, Status.NOT_DEFINED)
        self.assertIsInstance(reader.get_a('b').value, self.module.B)
        self.assertIsInstance(reader.get_a('b').value, self.module.A)
        self.assertEqual(reader.get_b('a').status, Status.NO_ENTRY)
        batch = reader.get_a_many(['a', 'missing', 'a']).value
        self.assertEqual(
            [r.value.id if isinstance(r, Success) else r.status for r in batch],
            ['a', Status.NO_ENTRY, 'a'],
        )
        self.assertIsInstance(reader.resolve(EntityRef('Other', 1)).value, self.module.Other)
        self.assertEqual(reader.get_a(1).status, Status.ERROR)
        self.assertEqual(reader.get_other(True).status, Status.ERROR)
        self.assertEqual(reader.resolve('not a ref').status, Status.ERROR)
        self.assertEqual(reader.get('unknown', 'a').status, Status.ERROR)
        self.assertEqual(reader.get_many('A', 'a').status, Status.ERROR)
        self.assertEqual(reader.get_many('A', [1]).status, Status.ERROR)
        self.assertEqual(reader.get_a_many([]), Success([]))
        self.assertEqual(reader.close(), Success(None))
        self.assertEqual(a.read_text().status, Status.ERROR)
        self.assertEqual(reader.get_a('a').status, Status.ERROR)
        self.assertEqual(reader.get_a_many([]).status, Status.ERROR)
        self.assertEqual(reader.get_a_many(['a']).status, Status.ERROR)

    def test_missing_empty_and_not_applicable_remain_distinct(self):
        self.put('A.text', state='EMPTY')
        self.put('A.limit', state='NOT_APPLICABLE')
        reader = self.open()
        a = reader.get_a('a').value
        self.assertEqual(a.read_text().status, Status.EMPTY)
        self.assertEqual(a.read_count().status, Status.NO_ENTRY)
        self.assertEqual(a.read_limit().status, Status.NOT_APPLICABLE)
        self.assertEqual(a.read_members().status, Status.NO_ENTRY)
        self.assertEqual(reader.get_a('b').value.read_text().status, Status.NO_ENTRY)

    def test_config_long_rows_preserve_types_states_and_field_identity(self):
        huge = '123456789012345678901234567890'
        self.put('A.limit', huge)
        self.put('A.enabled', 0)
        self.put('A.threshold', '0.12345678901234567890123456789')
        self.put('A.peer', '1')
        self.put('A.peers')
        members = self.layout.fields['A.peers'].members
        self.writer.execute(f'INSERT INTO {quote(members)} VALUES (?, ?, ?)', ('a', 'A.peers', '1'))
        self.writer.commit()
        reader = self.open()
        a = reader.get_a('a').value
        self.assertEqual(a.read_limit(), Success(int(huge)))
        self.assertEqual(a.read_enabled(), Success(False))
        self.assertEqual(a.read_threshold(), Success(Decimal('0.12345678901234567890123456789')))
        self.assertEqual(a.read_peer(), Success(EntityRef('Other', 1)))
        self.assertEqual(a.read_peers(), Success(frozenset({EntityRef('Other', 1)})))
        self.assertEqual(reader.get_c('c').value.read_limit(), Success(30, 'model_default'))
        self.assertEqual(
            self.writer.execute(
                'SELECT typeof(value), value FROM tb_a_config WHERE id = ? AND field = ?',
                ('a', 'A.limit'),
            ).fetchone(),
            ('text', huge),
        )
        self.assertEqual(
            self.writer.execute(
                'SELECT typeof(value), value FROM tb_a_config WHERE id = ? AND field = ?',
                ('a', 'A.enabled'),
            ).fetchone(),
            ('integer', 0),
        )
        reader.close()
        for identity, state in [('A.limit', 'EMPTY'), ('A.enabled', 'NOT_APPLICABLE')]:
            self.put(identity, state=state)
        reader = self.open()
        self.assertEqual(reader.get_a('a').value.read_limit().status, Status.EMPTY)
        self.assertEqual(reader.get_a('a').value.read_enabled().status, Status.NOT_APPLICABLE)
        reader.close()
        self.put('A.limit', state='NO_ENTRY')
        self.assertEqual(self.open().get_a('a').value.read_limit(), Success(20, 'model_default'))
        for identity, value in [('A.limit', 1), ('A.enabled', '1'), ('A.enabled', 2)]:
            with (
                self.subTest(identity=identity, value=value),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.put(identity, value)
        with self.assertRaises(sqlite3.IntegrityError):
            self.writer.execute("INSERT INTO tb_a_config VALUES ('b', 'A.unknown', 'VALUE', 'x')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.writer.execute(
                f'INSERT INTO {quote(members)} VALUES (?, ?, ?)', ('a', 'A.limit', '2')
            )

    def test_sparse_snapshots_scd_changes_gaps_and_collections(self):
        for day, value in [('2026-01-02', '10.1'), ('2026-01-04', 'bad')]:
            self.put('A.price', value, date=day, dim_source='Primary')
        self.put('A.price', '99', date='2026-01-02', dim_source='Secondary')
        self.put('A.positions', date='2026-01-02')
        self.put('A.outstanding', '10', valid_from='2026-01-02', valid_to='2026-01-04')
        self.put('A.outstanding', '20', valid_from='2026-01-04', valid_to='2026-01-05')
        self.put('A.outstanding', '30', valid_from='2026-01-07', valid_to='2999-12-31')
        self.put('A.owners', valid_from='1900-01-01', valid_to='2999-12-31')
        for name, values in [
            ('A.members', ('a', '1')),
            ('A.positions', ('a', '2026-01-02', '1')),
            ('A.owners', ('a', '1900-01-01', '99')),
        ]:
            if name == 'A.members':
                self.put(name)
            stored = self.layout.fields[name]
            self.writer.execute(
                f'INSERT INTO {quote(stored.members)} VALUES ({", ".join("?" for _ in values)})',
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.writer.execute(
                    f'INSERT INTO {quote(stored.members)} VALUES ({", ".join("?" for _ in values)})',
                    values,
                )
        self.writer.commit()
        reader = self.open()
        a = reader.get_a('a').value
        primary = self.module.SourcesCode('Primary')
        self.assertEqual(a.read_price(date(2026, 1, 2), source=primary), Success(Decimal('10.1')))
        self.assertEqual(a.read_price(date(2026, 1, 3), source=primary).status, Status.NO_ENTRY)
        self.assertEqual(
            a.read_price(date(2026, 1, 2), source=self.module.SourcesCode('Secondary')),
            Success(Decimal(99)),
        )
        result = a.read_price_range(date(2026, 1, 1), date(2026, 1, 5), source=primary).value
        self.assertEqual([d for d, _ in result], [date(2026, 1, 2), date(2026, 1, 4)])
        self.assertEqual(result[1][1].status, Status.ERROR)
        events = a.read_outstanding_range(date(2026, 1, 1), date(2026, 1, 8)).value
        self.assertEqual([d.day for d, _ in events], [1, 2, 4, 5, 7])
        self.assertEqual(
            [r.value if isinstance(r, Success) else r.status for _, r in events],
            [Status.NO_ENTRY, Decimal(10), Decimal(20), Status.NO_ENTRY, Decimal(30)],
        )
        self.assertEqual(a.read_outstanding(date(2999, 12, 31)).status, Status.NO_ENTRY)
        self.assertEqual(
            a.read_owners(date(1900, 1, 1)), Success(frozenset({EntityRef('Other', 99)}))
        )
        self.assertEqual(
            a.read_positions(date(2026, 1, 2)), Success(frozenset({EntityRef('Other', 1)}))
        )
        self.assertEqual(a.read_members(), Success(frozenset({EntityRef('Other', 1)})))
        self.assertEqual(
            a.read_price_range(date(2026, 1, 1), date(2026, 1, 1), source=primary), Success([])
        )
        self.assertEqual(
            a.read_price_range(date(2026, 1, 5), date(2026, 1, 1), source=primary).status,
            Status.ERROR,
        )
        self.assertEqual(a.read_price('yesterday', source=primary).status, Status.ERROR)
        self.assertEqual(a.read_price(date.today(), source='Primary').status, Status.ERROR)
        self.assertEqual(reader.read(a, 'A.price', {}, date.today()).status, Status.ERROR)
        self.assertEqual(
            reader.read_range(a, 'A.text', {}, date.today(), date.today()).status, Status.ERROR
        )
        self.assertEqual(
            reader.read_range(a, 'B.extra', {}, date.today(), date.today()).status,
            Status.NOT_DEFINED,
        )

    def test_overlaps_invalid_records_and_database_failures(self):
        self.put('A.outstanding', '10', valid_from='2026-01-01', valid_to='2026-01-05')
        self.put('A.outstanding', '20', valid_from='2026-01-02', valid_to='2026-01-06')
        for identity, value in [
            ('A.flag', 2),
            ('A.count', '01'),
            ('A.day', '20260101'),
            ('A.amount', 'Infinity'),
            ('A.currency', 'BAD'),
        ]:
            self.put(identity, value)
        reader = self.open()
        a = reader.get_a('a').value
        self.assertEqual(a.read_outstanding(date(2026, 1, 3)).status, Status.ERROR)
        self.assertEqual(
            a.read_outstanding_range(date(2026, 1, 3), date(2026, 1, 4)).value[0][1].status,
            Status.ERROR,
        )
        for name in ('flag', 'count', 'day', 'amount', 'currency'):
            self.assertEqual(getattr(a, 'read_' + name)().status, Status.ERROR)
        foreign = EntityHandle(self.open(), 'A', 'a')
        self.assertEqual(reader.read(foreign, 'A.text', {}).status, Status.ERROR)
        foreign.reader.close()
        with self.assertRaises(sqlite3.OperationalError):
            reader._connection.execute('DELETE FROM tb_yambl_metadata')
        reader.close()
        self.writer.execute(
            "UPDATE tb_yambl_metadata SET value = '1' WHERE key = 'layout_revision'"
        )
        self.writer.commit()
        self.assertIn('does not match', self.module.SampleReader.open(self.path).message)
        self.writer.execute('DELETE FROM tb_yambl_metadata')
        self.writer.commit()
        self.assertIn('does not match', self.module.SampleReader.open(self.path).message)
        self.assertEqual(
            self.module.SampleReader.open(self.root / 'missing.db').status, Status.ERROR
        )
        self.assertFalse((self.root / 'missing.db').exists())
        self.assertEqual(self.module.SampleReader.open(None).status, Status.ERROR)

    def test_decode_input_contracts_and_failures(self):
        reader = self.open()
        types = {s.field.name: s.field.type for s in self.layout.fields.values()}
        for name, value, expected in [
            ('flag', True, 1),
            ('amount', Decimal('12.30'), '12.30'),
            ('day', date(2026, 1, 1), '2026-01-01'),
            ('other', EntityRef('Other', 1), '1'),
        ]:
            self.assertEqual(reader._encode(types[name], value), expected)
        for name, value in [
            ('flag', 'true'),
            ('amount', 2.5),
            ('day', '2026-01-01'),
            ('other', EntityRef('A', 'a')),
            ('other', 1),
        ]:
            with self.assertRaisesRegex(ValueError, 'invalid input|reference'):
                reader._encode(types[name], value)
        for name, value in [('text', b'bad'), ('amount', '12345.00'), ('amount', '1.001')]:
            with self.assertRaises(ValueError):
                reader._decode(types[name], value)
        with patch.object(reader, '_connection') as connection:
            connection.execute.side_effect = sqlite3.OperationalError('query failed')
            a = EntityHandle(reader, 'A', 'a')
            self.assertEqual(reader.read(a, 'A.text', {}).status, Status.ERROR)
            self.assertEqual(
                reader.read_range(
                    a,
                    'A.price',
                    {'source': self.module.SourcesCode('Primary')},
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                ).status,
                Status.ERROR,
            )
            connection.close.side_effect = sqlite3.OperationalError('close failed')
            self.assertEqual(reader.close().status, Status.ERROR)

    def test_corruption_and_exact_constraints(self):
        from dataclasses import replace

        from yambl_compiler.ir.model import Value

        self.writer.execute('PRAGMA ignore_check_constraints = ON')
        self.put('A.text', 'bad', state='CORRUPT')
        self.writer.execute('UPDATE tb_a SET entity_type = ? WHERE id = ?', ('unknown', 'b'))
        self.writer.commit()
        reader = self.open()
        self.assertEqual(reader.get_a('b').status, Status.ERROR)
        self.assertEqual(reader.get_a('a').value.read_text().status, Status.ERROR)
        integer = self.layout.fields['A.count'].field.type
        exact = replace(integer, exact=Value('integer', '1', ''))
        self.assertEqual(reader._decode(exact, '1'), 1)
        with self.assertRaisesRegex(ValueError, 'exact'):
            reader._decode(exact, '2')
        boolean = replace(self.layout.fields['A.flag'].field.type, exact=Value('bool', 'true', ''))
        self.assertEqual(reader._decode(boolean, 1), True)
        reference = replace(
            self.layout.fields['A.other'].field.type, exact=Value('reference', '1', 'Other')
        )
        self.assertEqual(reader._decode(reference, '1'), EntityRef('Other', 1))
        with self.assertRaisesRegex(ValueError, 'exact'):
            reader._decode(reference, '2')

    def test_database_enforces_scd_bounds_and_collection_header(self):
        for start, end in [
            (None, '2026-01-01'),
            ('2026-01-01', None),
            ('1899-12-31', '2026-01-01'),
            ('2026-01-01', '3000-01-01'),
            ('2026-01-01', '2026-01-01'),
            ('2026-01-02', '2026-01-01'),
        ]:
            with self.subTest(start=start, end=end), self.assertRaises(sqlite3.IntegrityError):
                self.put('A.outstanding', '1', valid_from=start, valid_to=end)
        table = self.layout.fields['A.members'].members
        with self.assertRaises(sqlite3.IntegrityError):
            self.writer.execute(
                f'INSERT INTO {quote(table)} VALUES (?, ?)', ('missing-header', '1')
            )

    def test_missing_columns_are_results_not_exceptions(self):
        table = self.layout.fields['A.text'].table
        self.writer.execute(f'DROP TABLE {quote(table)}')
        self.writer.execute(f'CREATE TABLE {quote(table)} (id TEXT, entity_type TEXT)')
        self.writer.execute(f'INSERT INTO {quote(table)} VALUES (?, ?)', ('a', 'A'))
        self.writer.commit()
        reader = self.open()
        self.assertEqual(reader.get_a('a').value.read_text().status, Status.ERROR)
