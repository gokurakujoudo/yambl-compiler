"""Exercise the combined language features against real generated repositories."""

import os
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.writing import dumps


class IntegratedFeatureTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    async def test_combined_features_and_transaction_failures(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
hierarchy Sources { - 1 'Primary' - 2 'Secondary' }
dimension feed { value source: string hierarchy order: Hierarchy = Sources }
@unique_by(self.market, self.symbol)
@unique_by(self.external)
entity Instrument {
    id id: string
    attr market: string
    attr symbol: string
    attr external: string
    attr status: one_of['open', 'closed']
    attr created: datetime
    attr zone: code<TimeZones>
    @dimension(feed) @temporal(day: date) snapshot attr price: decimal
    constraint prices {
        let history = self.price::records()
        assert history.all(r ->
            r.value > 0
        )
    }
}
entity Bond inherits Instrument {
    @override attr status: exact<'open'>
    constraint bond_prices { assert self.price::records().all(r -> r.value < 1000) }
}
entity A { id id: string attr limit: integer @relationship(one, maybe) attr b: B }
entity B { id id: string @relationship(one, maybe) attr c: C }
entity C {
    id id: string
    @reverse_relationship(A.b.c) attr a: A
    constraint linked { assert self.a::value().limit::value() > 0 }
}
''',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')

        def plan(*operations):
            return Plan('integrated', None, (Group('g', operations),))

        initial = plan(
            Operation(
                'insert',
                'Bond',
                ('id', 'market', 'symbol', 'external', 'status', 'created', 'zone'),
                (
                    (
                        'bond',
                        'M',
                        'B',
                        'X',
                        'open',
                        '2026-01-01T08:00:00+08:00',
                        Symbol('builtins.TimeZones.UTC'),
                    ),
                ),
            ),
            Operation(
                'insert', 'Instrument', ('id', 'market', 'status'), (('empty', 'M', 'closed'),)
            ),
            Operation(
                'replace',
                'Instrument.price',
                ('source', 'day', 'value'),
                (
                    ('Primary', '2026-01-01', Decimal('10')),
                    ('Secondary', '2026-01-01', Decimal('12')),
                ),
                (Condition('id', 'bond'),),
            ),
            Operation('insert', 'C', ('id',), (('c',),)),
            Operation('insert', 'B', ('id', 'c'), (('b', 'c'),)),
            Operation('insert', 'B', ('id',), (('b2',),)),
            Operation('insert', 'A', ('id', 'limit', 'b'), (('a', 1, 'b'),)),
        )
        initial_path = self.write('integrated.yaplan', dumps(initial))
        values = []
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    applied = store.apply(initial_path)
                    self.assertEqual(applied.status, 'committed')
                    with closing(store.read()) as reader:
                        bond = reader.get_bond('bond').value
                        self.assertEqual(
                            bond.created.value, datetime(2026, 1, 1, tzinfo=timezone.utc)
                        )
                        selected = bond.read_price_best_available(day=date(2026, 1, 1)).value
                        self.assertEqual(
                            (selected.value, selected.dimensions.source), (Decimal('10'), 'Primary')
                        )
                        self.assertEqual(reader.get_c('c').value.read_a().value.id, 'a')
                        self.assertTrue(reader.validate().value.valid)
                        values.append(
                            (
                                bond.created.value,
                                selected.value,
                                selected.dimensions.source,
                                reader.get_c('c').value.read_a().value.id,
                            )
                        )
                    before = store.snapshot().tables
                    failures = (
                        (
                            plan(
                                Operation('update', 'Bond', ('id', 'status'), (('bond', 'closed'),))
                            ),
                            'exact',
                        ),
                        (
                            plan(
                                Operation(
                                    'insert',
                                    'Instrument',
                                    ('id', 'market', 'symbol'),
                                    (('dup', 'M', 'B'),),
                                )
                            ),
                            'Instrument.market',
                        ),
                        (
                            plan(
                                Operation(
                                    'insert', 'Instrument', ('id', 'external'), (('dup', 'X'),)
                                )
                            ),
                            'Instrument.external',
                        ),
                        (
                            plan(
                                Operation(
                                    'update',
                                    'Instrument',
                                    ('id', 'status'),
                                    (('empty', 'invalid'),),
                                )
                            ),
                            'one_of',
                        ),
                        (
                            plan(
                                Operation(
                                    'replace',
                                    'Instrument.price',
                                    ('source', 'day', 'value'),
                                    (('Primary', '2026-01-01', Decimal('-1')),),
                                    (Condition('id', 'bond'),),
                                )
                            ),
                            'Instrument::constraint.prices',
                        ),
                        (
                            plan(
                                Operation(
                                    'replace',
                                    'Instrument.price',
                                    ('source', 'day', 'value'),
                                    (('Primary', '2026-01-01', Decimal('1000')),),
                                    (Condition('id', 'bond'),),
                                )
                            ),
                            'Bond::constraint.bond_prices',
                        ),
                        (
                            plan(Operation('update', 'A', ('id', 'limit'), (('a', 0),))),
                            'C::constraint.linked',
                        ),
                        (
                            plan(Operation('update', 'B', ('id', 'c'), (('b2', 'c'),))),
                            'relationship target has multiple sources',
                        ),
                    )
                    for index, (invalid, diagnostic) in enumerate(failures):
                        with self.assertRaises(ValueError) as caught:
                            store.apply(
                                self.write(f'{backend}-invalid-{index}.yaplan', dumps(invalid))
                            )
                        self.assertIn(diagnostic, str(caught.exception))
                        if diagnostic == 'Instrument::constraint.prices':
                            issue = caught.exception.issues[0]
                            self.assertEqual(issue.status, 'false')
                            self.assertIn('\n', issue.assert_source)
                            self.assertEqual(
                                issue.dimensions, (('source', 'Primary'), ('day', '2026-01-01'))
                            )
                        self.assertEqual(store.snapshot().tables, before)
                    restored = store.rollback(applied.path)
                    self.assertEqual(restored.status, 'committed')
                    self.assertEqual(store.rollback(restored.path).status, 'committed')
                    with closing(store.read()) as reader:
                        self.assertEqual(reader.get_c('c').value.read_a().value.id, 'a')
                        self.assertTrue(reader.validate().value.valid)
                code = """import sys
sys.path.insert(0, sys.argv[1])
from sample_model import SampleData
assert not any(name == 'yambl_compiler' or name.startswith('yambl_compiler.') for name in sys.modules)
assert SampleData is not None
"""
                result = subprocess.run(
                    [sys.executable, '-I', '-X', 'utf8', '-c', code, str(output)],
                    cwd=self.root,
                    env={**os.environ, 'PYTHONUTF8': '1'},
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values[0], values[1])
