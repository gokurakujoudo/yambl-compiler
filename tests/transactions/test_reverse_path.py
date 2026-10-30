"""Exercise multi-hop reverse relationships on both generated backends."""

import unittest
from contextlib import closing
from datetime import date

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status
from yambl_compiler.transactions.snapshot import SnapshotBackend


class ReversePathTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    async def test_constraint_and_unique_key_read_the_candidate_chain(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
entity A { id id: string @relationship(one, maybe) attr b: B }
entity B { id id: string @relationship(one, maybe) attr c: C }
@unique_by(self.a)
entity C {
    id id: string
    @reverse_relationship(A.b.c) attr a: A
    constraint linked { assert self.a::value().id::value() == 'a' }
}
''',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        initial = Plan(
            'initial',
            None,
            (
                Group(
                    'g',
                    (
                        Operation('insert', 'C', ('id',), (('c',),)),
                        Operation('insert', 'B', ('id', 'c'), (('b', 'c'),)),
                        Operation('insert', 'B', ('id',), (('b2',),)),
                        Operation('insert', 'A', ('id', 'b'), (('a', 'b'),)),
                    ),
                ),
            ),
        )
        broken = Plan(
            'broken', None, (Group('g', (Operation('update', 'A', ('id', 'b'), (('a', 'b2'),)),)),)
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(initial).status, 'committed')
                    before = store.snapshot().tables
                    with self.assertRaises(ValueError) as caught:
                        store.apply(broken)
                    self.assertTrue(
                        any(
                            issue.constraint == 'C::constraint.linked'
                            and issue.status == 'NO_ENTRY'
                            for issue in caught.exception.issues
                        )
                    )
                    self.assertEqual(store.snapshot().tables, before)

    async def test_temporal_and_dimensional_hops_share_the_requested_coordinate(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
dimension feed { value source: string }
entity A {
    id id: string
    @dimension(feed) @temporal(day: date) @relationship(one, maybe) snapshot attr b: B
    @dimension(feed) @temporal(day: date) @relationship(one, maybe) scd attr historical_b: B
}
entity B {
    id id: string
    @dimension(feed) @temporal(day: date) @relationship(one, maybe) snapshot attr c: C
    @dimension(feed) @temporal(day: date) @relationship(one, maybe) scd attr historical_c: C
}
entity C {
    id id: string
    @dimension(feed) @temporal(day: date) @reverse_relationship(A.b.c) snapshot attr a: A
    @dimension(feed) @temporal(day: date) @reverse_relationship(A.historical_b.historical_c) scd attr historical_a: A
}
''',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        plan = Plan(
            'time',
            None,
            (
                Group(
                    'g',
                    (
                        Operation('insert', 'A', ('id',), (('a',),)),
                        Operation('insert', 'B', ('id',), (('b',),)),
                        Operation('insert', 'C', ('id',), (('c',),)),
                        Operation(
                            'replace',
                            'A.b',
                            ('source', 'day', 'value'),
                            (('feed', '2026-01-01', 'b'), ('feed', '2026-01-02', 'b')),
                            (Condition('id', 'a'),),
                        ),
                        Operation(
                            'replace',
                            'B.c',
                            ('source', 'day', 'value'),
                            (('feed', '2026-01-02', 'c'),),
                            (Condition('id', 'b'),),
                        ),
                        Operation(
                            'replace',
                            'A.historical_b',
                            ('source', 'valid_from', 'valid_to', 'value'),
                            (('feed', '2026-01-01', '2026-01-10', 'b'),),
                            (Condition('id', 'a'),),
                        ),
                        Operation(
                            'replace',
                            'B.historical_c',
                            ('source', 'valid_from', 'valid_to', 'value'),
                            (('feed', '2026-01-03', '2026-01-05', 'c'),),
                            (Condition('id', 'b'),),
                        ),
                    ),
                ),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(plan).status, 'committed')
                    with closing(store.read()) as reader:
                        c = reader.get_c('c').value
                        self.assertEqual(
                            c.read_a(date(2026, 1, 1), source='feed').status, Status.NO_ENTRY
                        )
                        self.assertEqual(c.read_a(date(2026, 1, 2), source='feed').value.id, 'a')
                        self.assertEqual(
                            c.read_a(date(2026, 1, 2), source='other').status, Status.NO_ENTRY
                        )
                        self.assertEqual(
                            c.read_historical_a(date(2026, 1, 2), source='feed').status,
                            Status.NO_ENTRY,
                        )
                        self.assertEqual(
                            c.read_historical_a(date(2026, 1, 4), source='feed').value.id, 'a'
                        )
                        self.assertEqual(
                            c.read_historical_a(date(2026, 1, 5), source='feed').status,
                            Status.NO_ENTRY,
                        )
                        records = c.historical_a.value
                        self.assertEqual(
                            [(r.valid_from, r.valid_to, r.value.value.id) for r in records],
                            [(date(2026, 1, 3), date(2026, 1, 5), 'a')],
                        )

    async def test_two_and_three_hops_and_middle_ambiguity(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
entity A { id id: string @relationship(one, maybe) attr b: B }
entity B { id id: string @relationship(one, maybe) attr c: C }
entity C { id id: string @relationship(one, maybe) attr d: D @reverse_relationship(A.b.c) attr a: A }
entity D { id id: string @reverse_relationship(A.b.c.d) attr a: A }
''',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')

        def plan(*operations):
            return Plan('reverse', None, (Group('g', operations),))

        initial = plan(
            Operation('insert', 'D', ('id',), (('d',), ('empty',))),
            Operation('insert', 'C', ('id', 'd'), (('c', 'd'),)),
            Operation('insert', 'B', ('id', 'c'), (('b', 'c'),)),
            Operation('insert', 'B', ('id',), (('b2',),)),
            Operation('insert', 'A', ('id', 'b'), (('a', 'b'),)),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(initial).status, 'committed')
                    with closing(store.read()) as reader:
                        self.assertEqual(reader.get_c('c').value.read_a().value.id, 'a')
                        self.assertEqual(reader.get_d('d').value.read_a().value.id, 'a')
                        self.assertEqual(
                            reader.get_d('empty').value.read_a().status, Status.NO_ENTRY
                        )
                    changed = store.apply(
                        plan(Operation('update', 'A', ('id', 'b'), (('a', 'b2'),)))
                    )
                    with closing(store.read()) as reader:
                        self.assertEqual(reader.get_d('d').value.read_a().status, Status.NO_ENTRY)
                    restored = store.rollback(changed.path)
                    with closing(store.read()) as reader:
                        self.assertEqual(reader.get_d('d').value.read_a().value.id, 'a')
                    store.rollback(restored.path)
                    with closing(store.read()) as reader:
                        self.assertEqual(reader.get_d('d').value.read_a().status, Status.NO_ENTRY)

                    tables = store.snapshot().tables
                    b2 = next(row for row in tables['tb_b'] if row['id'] == 'b2')
                    b2['c__state'], b2['c'] = 0, 'c'
                    with module.SampleReader(SnapshotBackend(tables), store.layout) as reader:
                        self.assertEqual(reader.get_d('d').value.read_a().status, Status.ERROR)
