"""Exercise finite scalar contracts through generated data repositories."""

import unittest
from contextlib import closing
from datetime import datetime, timezone

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success
from yambl_compiler.transactions.writing import dumps


class FiniteTransactionTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    async def test_hierarchy_singleton_applies_to_subtype_on_both_backends(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            "hierarchy H { - 1 'x' } hierarchy Other { - 1 'y' } "
            'entity A { id id: string attr h: Hierarchy } '
            'entity B inherits A { @override attr h: exact<H> }',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                good = self.write(
                    'good.yaplan',
                    dumps(
                        Plan(
                            'good',
                            None,
                            (Group('g', (Operation('insert', 'B', ('id', 'h'), (('b', 'H'),)),)),),
                        )
                    ),
                )
                bad = self.write(
                    'bad.yaplan',
                    dumps(
                        Plan(
                            'bad',
                            None,
                            (
                                Group(
                                    'g',
                                    (Operation('insert', 'B', ('id', 'h'), (('other', 'Other'),)),),
                                ),
                            ),
                        )
                    ),
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(good).status, 'committed')
                    with closing(store.read()) as reader:
                        self.assertEqual(reader.get_b('b').value.h.value.id, 'H')
                    before = store.snapshot().tables
                    with self.assertRaisesRegex(ValueError, 'exact'):
                        store.apply(bad)
                    self.assertEqual(store.snapshot().tables, before)

    async def test_both_backends_enforce_finite_inputs(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """decodes D { - 1 One: 'one' - 2 Two: 'two' - 3 Three: 'three' }
entity A {
    id id: string
    static attr status: one_of['open', 'closed']
    static attr number: one_of[2, 3]
    static attr code: one_of[D.One, D.Two]
    static attr moment: one_of[parse_datetime('2026-01-01T00:00:00Z'), parse_datetime('2026-01-02T00:00:00Z')]
    static attr fixed: exact<1 + 2>
    static attr instant: exact<parse_datetime('2026-01-01T00:00:00Z')>
    computed attr result: one_of['yes', 'no'] { return 'yes' }
    lookup by_status { status = self.status::value() }
}
""",
        )
        model = compile_repository(self.root)
        output = self.root / 'generated'
        generate(model, output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        fields = ('id', 'status', 'number', 'code', 'moment', 'fixed', 'instant')
        valid = (
            'a',
            'open',
            2,
            Symbol('D.One'),
            '2026-01-01T08:00:00+08:00',
            3,
            '2026-01-01T08:00:00+08:00',
        )
        insert = Operation('insert', 'A', fields, (valid,))
        good = self.write('good.yaplan', dumps(Plan('good', None, (Group('g', (insert,)),))))
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(good).status, 'committed')
                    with closing(store.read()) as reader:
                        entity = reader.get_a('a').value
                        self.assertEqual(entity.status, Success('open'))
                        self.assertEqual(
                            entity.moment, Success(datetime(2026, 1, 1, tzinfo=timezone.utc))
                        )
                        self.assertEqual(entity.read_result(), Success('yes', 'computation'))
                        matcher = reader.prepare_a_by_status().value
                        self.assertEqual(
                            matcher.match_many([module.AByStatusKey(status='open')]).value,
                            (Success('a'),),
                        )
                        self.assertEqual(
                            matcher.match_many([module.AByStatusKey(status='invalid')])
                            .value[0]
                            .status,
                            Status.ERROR,
                        )
                    before = store.snapshot().tables
                    for index, invalid in (
                        (1, 'invalid'),
                        (2, 4),
                        (3, Symbol('D.Three')),
                        (4, '2026-01-03T00:00:00Z'),
                        (5, 4),
                        (6, '2026-01-03T00:00:00Z'),
                    ):
                        values = list(valid)
                        values[0] = 'b'
                        values[index] = invalid
                        operation = Operation('insert', 'A', fields, (tuple(values),))
                        bad = self.write(
                            'bad.yaplan', dumps(Plan('bad', None, (Group('g', (operation,)),)))
                        )
                        with self.assertRaisesRegex(ValueError, '(one_of|exact) constraint'):
                            store.apply(bad)
                        self.assertEqual(store.snapshot().tables, before)
