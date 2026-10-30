"""Read generated literal calculations through both real storage backends."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository, validate_ir
from yambl_compiler.ir.codec import dumps, loads
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.computing import literal_mapping
from yambl_compiler.storage.entity_validation import value_key
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import EntityRef, Status
from yambl_compiler.storage.value_types import generate_values


class LiteralRuntimeTests(FilesystemCase):
    """Verify context types survive IR loading and generated execution."""

    def test_literal_package(self) -> None:
        """Compile and read typed literals, constants and strict conversions.

        :returns: None.
        """
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'a.yambl',
            """
            const LABEL: string = 'Other'
            const RATE: decimal = truncate(1.239, scale=2)
            decodes Codes { - 1 Usd: 'Dollar' }
            value amount { static attr quantity: decimal static attr label: string }
            value nested { static attr payload: amount static attr owner: Thing static attr code: code<Codes> }
            value timing { static attr day: date static attr instant: datetime }
            const AMOUNTS: values<amount> = [{quantity: 1.25, label: LABEL}]
            entity Thing {
                id key: string
                computed attr amount: amount { let a: decimal = 1.25 return {quantity: a, label: LABEL} }
                computed attr numbers: values<integer> { return [1, 2, 2] }
                computed attr amounts: values<amount> { return AMOUNTS }
                computed attr recovered: values<integer> { return NO_ENTRY.recover(case NO_ENTRY -> []) }
                computed attr empty: mapping<string, decimal> { return {} }
                computed attr refs: collection<Thing> { return ['a', 'b'] }
                computed attr day: date { return '2026-01-01' }
                computed attr instant: datetime { return '2026-01-01T08:00:00+08:00' }
                computed attr conditional: values<integer> { return if true then [] else [1] }
                computed attr ratio: float { return RATE / 2 }
                computed attr floor_value: integer { return -7 // 3 }
                computed attr bad: integer { return to_integer('1.2') }
                computed attr formatted: string { return format_date(parse_date('2026-01-01')) }
                computed attr code: code<Codes> { return 'Usd' }
                computed attr selected: decimal { let a: amount = {quantity: 2, label: 'X'} return a.quantity }
            }
        """,
        )
        model = loads(dumps(compile_repository(self.root)))
        self.assertEqual(validate_ir(model), ())
        output = self.root / 'generated'
        layout = generate(model, output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        path = self.root / 'sample.db'
        with closing(sqlite3.connect(path)) as database:
            database.executescript((output / 'schema.sql').read_text())
            database.execute("INSERT INTO tb_thing(id) VALUES ('a')")
            database.commit()
            database.row_factory = sqlite3.Row
            rows = {
                t.name: [dict(row) for row in database.execute(f'SELECT * FROM "{t.name}"')]
                for t in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            target = files / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8', newline='\n')
        for opened in (
            module.SampleReader.open_sqlite(path),
            module.SampleReader.open_files(files),
        ):
            reader = opened.value
            self.addCleanup(reader.close)
            thing = reader.get_thing('a').value
            self.assertEqual(
                thing.read_amount().value, module.amount(quantity=Decimal('1.25'), label='Other')
            )
            amount_type = next(
                field.type
                for d in model.definitions
                if d.identity == 'Thing'
                for field in d.fields
                if field.name == 'amount'
            )
            first = value_key(thing.read_amount().value, amount_type, reader)
            same = value_key(
                module.amount(quantity=Decimal('1.250'), label='Other'), amount_type, reader
            )
            different = value_key(
                module.amount(quantity=Decimal('1.25'), label='Different'), amount_type, reader
            )
            self.assertEqual(first, same)
            self.assertNotEqual(first, different)
            self.assertEqual(thing.read_numbers().value, frozenset((1, 2)))
            self.assertEqual(
                thing.read_amounts().value,
                frozenset((module.amount(quantity=Decimal('1.25'), label='Other'),)),
            )
            self.assertEqual(thing.read_recovered().value, frozenset())
            self.assertEqual(dict(thing.read_empty().value), {})
            self.assertEqual(
                thing.read_refs().value,
                frozenset((EntityRef('Thing', 'a'), EntityRef('Thing', 'b'))),
            )
            self.assertEqual(thing.read_day().value, date(2026, 1, 1))
            self.assertEqual(thing.read_instant().value, datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(thing.read_conditional().value, frozenset())
            instant_type = next(
                f.type for d in model.definitions for f in d.fields if f.identity == 'Thing.instant'
            )
            for invalid in ('2026-01-01', datetime(2026, 1, 1)):
                with self.assertRaises(ValueError):
                    reader._computing.boundary(instant_type, invalid)
            self.assertEqual(thing.read_ratio().value, 0.615)
            self.assertEqual(thing.read_floor_value().value, -3)
            self.assertEqual(thing.read_bad().status, Status.ERROR)
            self.assertEqual(thing.read_formatted().value, '2026-01-01')
            self.assertEqual(thing.read_code().value, module.CodesCode.Usd)
            self.assertEqual(thing.read_selected().value, Decimal(2))
            type_ = next(
                f.type for d in model.definitions for f in d.fields if f.identity == 'Thing.amount'
            )
            with self.assertRaises(ValueError):
                reader._computing.boundary(type_, 'not a value')
        with self.assertRaises(ValueError):
            literal_mapping('a', 1, 'a', 2)
        value = next(d for d in model.definitions if d.identity == 'amount')
        for invalid in (
            replace(value, name='class'),
            replace(value, fields=(replace(value.fields[0], name='class'),)),
            replace(
                value,
                fields=(
                    replace(
                        value.fields[0], type=replace(value.fields[0].type, name='unsupported')
                    ),
                ),
            ),
        ):
            with self.subTest(value=invalid.name):
                with self.assertRaises(ValueError):
                    generate_values(replace(model, definitions=(invalid,)))
