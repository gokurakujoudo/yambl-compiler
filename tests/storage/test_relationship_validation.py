"""Check relationship cardinalities through both generated data backends."""

import asyncio

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.writing import dumps

SOURCE = """namespace core
entity Target { id key: string }
entity Source {
    id key: string
    @relationship(one, one) static attr target: Target
}
entity Basket {
    id key: string
    @relationship(one, many) static attr targets: collection<Target>
}
entity Rare {
    id key: string
    @relationship(one, one) @sparse('links') static attr target: Target
}
entity Parent { id key: string }
entity Child inherits Parent {
    @relationship(one, one) @dense('links') static attr target: Target
}
entity Sibling inherits Parent { @dense('links') static attr note: string }
entity History {
    id key: string
    @relationship(one, maybe) @temporal(day: date) scd attr target: Target
}
dimension location { value region: string }
entity Observation {
    id key: string
    @relationship(one, maybe) @dimension(location) @temporal(day: date)
    snapshot attr target: Target
}
entity RequiredObservation {
    id key: string
    @relationship(many, one) @temporal(day: date) snapshot attr target: Target
}
"""


class RelationshipValidationTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        self.layout = generate(compile_repository(self.root), self.root / 'generated', 'rel_model')
        self.model = load_generated(self.root / 'generated' / 'rel_model', 'rel_model')

    def plan(self, name, *operations):
        plan = Plan(name, self.layout.model.model_digest, (Group('changes', operations),))
        return self.write(name + '.yaplan', dumps(plan))

    def test_required_and_unique_static_relationships_on_both_backends(self):
        targets = Operation('insert', 'core.Target', ('key',), (('t1',), ('t2',)))
        missing = self.plan(
            'missing', targets, Operation('insert', 'core.Source', ('key',), (('s1',),))
        )
        duplicate = self.plan(
            'duplicate',
            targets,
            Operation('insert', 'core.Source', ('key', 'target'), (('s1', 't1'), ('s2', 't1'))),
        )
        empty = self.plan(
            'empty',
            targets,
            Operation('insert', 'core.Basket', ('key', 'targets'), (('b1', ()),)),
        )
        sparse_missing = self.plan(
            'sparse_missing', targets, Operation('insert', 'core.Rare', ('key',), (('r1',),))
        )
        valid = self.plan(
            'valid',
            targets,
            Operation('insert', 'core.Source', ('key', 'target'), (('s1', 't1'), ('s2', 't2'))),
            Operation('insert', 'core.Basket', ('key', 'targets'), (('b1', ('t1', 't2')),)),
            Operation('insert', 'core.Child', ('key', 'target'), (('c1', 't1'),)),
            Operation('insert', 'core.Sibling', ('key', 'note'), (('sibling', 'ok'),)),
        )
        staged = self.plan(
            'staged',
            targets,
            Operation('insert', 'core.Source', ('key',), (('s1',),)),
            Operation(
                'replace',
                'core.Source.target',
                ('value',),
                (('t1',),),
                (Condition('key', 's1'),),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with asyncio.run(self.model.RelData.create(config)) as store:
                    before = store.snapshot().tables
                    for plan in (missing, duplicate, empty, sparse_missing):
                        with self.assertRaisesRegex(ValueError, 'relationship'):
                            store.apply(plan)
                        self.assertEqual(store.snapshot().tables, before)
                    staged_result = store.apply(staged)
                    self.assertEqual(staged_result.status, 'committed')
                    self.assertEqual(store.rollback(staged_result.path).status, 'committed')
                    applied = store.apply(valid)
                    self.assertEqual(applied.status, 'committed')
                    with store.read() as reader:
                        self.assertTrue(reader.validate().value.valid)
                    after = store.snapshot().tables
                    rollback = store.rollback(applied.path)
                    self.assertEqual(rollback.status, 'committed')
                    self.assertEqual(store.snapshot().tables, before)
                    self.assertEqual(store.rollback(rollback.path).status, 'committed')
                    self.assertEqual(store.snapshot().tables, after)

    def test_scd_unique_target_uses_overlapping_intervals(self):
        create = self.plan(
            'create',
            Operation('insert', 'core.Target', ('key',), (('t1',),)),
            Operation('insert', 'core.History', ('key',), (('h1',), ('h2',))),
            Operation(
                'replace',
                'core.History.target',
                ('valid_from', 'valid_to', 'value'),
                (('2026-01-01', '2026-01-15', 't1'), ('2026-01-15', '2026-02-01', 't1')),
                (Condition('key', 'h1'),),
            ),
            Operation(
                'replace',
                'core.History.target',
                ('valid_from', 'valid_to', 'value'),
                (('2026-02-01', '2026-03-01', 't1'), ('2026-03-01', '2026-04-01', Symbol('EMPTY'))),
                (Condition('key', 'h2'),),
            ),
        )
        overlap = self.plan(
            'overlap',
            Operation(
                'replace',
                'core.History.target',
                ('valid_from', 'valid_to', 'value'),
                (('2026-01-15', '2026-02-01', 't1'),),
                (Condition('key', 'h2'),),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}-scd.lclcfg', f"backend: '{backend}'\npath: '{backend}-scd.data'\n"
                )
                with asyncio.run(self.model.RelData.create(config)) as store:
                    store.apply(create)
                    before = store.snapshot().tables
                    with self.assertRaisesRegex(ValueError, 'relationship'):
                        store.apply(overlap)
                    self.assertEqual(store.snapshot().tables, before)

    def test_snapshot_uniqueness_is_scoped_to_day_and_dimension(self):
        create = self.plan(
            'snapshot_create',
            Operation('insert', 'core.Target', ('key',), (('t1',),)),
            Operation('insert', 'core.Observation', ('key',), (('o1',), ('o2',))),
            Operation(
                'replace',
                'core.Observation.target',
                ('region', 'day', 'value'),
                (('north', '2026-01-01', 't1'),),
                (Condition('key', 'o1'),),
            ),
            Operation(
                'replace',
                'core.Observation.target',
                ('region', 'day', 'value'),
                (('south', '2026-01-01', 't1'),),
                (Condition('key', 'o2'),),
            ),
        )
        conflict = self.plan(
            'snapshot_conflict',
            Operation(
                'replace',
                'core.Observation.target',
                ('region', 'day', 'value'),
                (('north', '2026-01-01', 't1'),),
                (Condition('key', 'o2'),),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}-snapshot.lclcfg',
                    f"backend: '{backend}'\npath: '{backend}-snapshot.data'\n",
                )
                with asyncio.run(self.model.RelData.create(config)) as store:
                    store.apply(create)
                    before = store.snapshot().tables
                    with self.assertRaisesRegex(ValueError, 'relationship'):
                        store.apply(conflict)
                    self.assertEqual(store.snapshot().tables, before)

    def test_temporal_required_checks_existing_coordinates_only(self):
        create = self.plan(
            'temporal_create',
            Operation('insert', 'core.Target', ('key',), (('t1',),)),
            Operation('insert', 'core.RequiredObservation', ('key',), (('o1',),)),
        )
        missing = self.plan(
            'temporal_empty',
            Operation(
                'replace',
                'core.RequiredObservation.target',
                ('day', 'value'),
                (('2026-01-01', Symbol('EMPTY')),),
                (Condition('key', 'o1'),),
            ),
        )
        value = self.plan(
            'temporal_value',
            Operation(
                'replace',
                'core.RequiredObservation.target',
                ('day', 'value'),
                (('2026-01-01', 't1'),),
                (Condition('key', 'o1'),),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}-required.lclcfg',
                    f"backend: '{backend}'\npath: '{backend}-required.data'\n",
                )
                with asyncio.run(self.model.RelData.create(config)) as store:
                    store.apply(create)
                    before = store.snapshot().tables
                    with self.assertRaisesRegex(ValueError, 'relationship'):
                        store.apply(missing)
                    self.assertEqual(store.snapshot().tables, before)
                    self.assertEqual(store.apply(value).status, 'committed')
