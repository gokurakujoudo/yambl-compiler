"""Exercise cycle rules through real generated transactions on both backends."""

import asyncio

from tests.storage.test_validation import SOURCE
from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.writing import dumps


class CycleTransactionTests(FilesystemCase):
    """Verify actual apply and rollback boundaries without touching playground data."""

    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        source = SOURCE.replace('snapshot attr snap:', '@temporal(day: date) snapshot attr snap:')
        source = source.replace('scd attr period:', '@temporal(day: date) scd attr period:')
        self.write('model.yambl', source)
        self.layout = generate(
            compile_repository(self.root), self.root / 'generated', 'sample_model'
        )
        self.model = load_generated(self.root / 'generated' / 'sample_model', 'sample_model')

    def plan(self, name, *operations):
        """Publish a named test Plan with the actual compiled model digest."""
        plan = Plan(name, self.layout.model.model_digest, (Group('changes', operations),))
        return self.write(f'{name}.yaplan', dumps(plan))

    def stores(self):
        """Create isolated file and SQLite repositories with generated interfaces."""
        for backend in ('files', 'sqlite'):
            config = self.write(
                f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
            )
            store = asyncio.run(self.model.SampleData.create(config))
            self.addCleanup(store.close)
            yield backend, store

    def rejected_without_changes(self, store, path):
        """Reject a cycle without publishing rows or a transaction."""
        before = store.snapshot().tables
        transactions = sorted(store.transaction_directory().glob('*.yatran'))
        with self.assertRaisesRegex(ValueError, 'same-field reference cycle'):
            store.apply(path)
        self.assertEqual(store.snapshot().tables, before)
        self.assertEqual(sorted(store.transaction_directory().glob('*.yatran')), transactions)

    def apply_and_undo(self, store, path):
        """Apply a valid Plan and roll it back to its complete prior state."""
        before = store.snapshot().tables
        applied = store.apply(path)
        self.assertEqual(applied.status, 'committed')
        self.assertTrue(applied.path.is_file())
        restored = store.rollback(applied.path)
        self.assertEqual(restored.status, 'committed')
        self.assertTrue(restored.path.is_file())
        self.assertEqual(store.snapshot().tables, before)

    def test_inserted_target_activates_existing_dangling_edge(self):
        seed = self.plan('seed', Operation('insert', 'Node', ('key', 'parent'), (('a', 'x'),)))
        cycle = self.plan('cycle', Operation('insert', 'Node', ('key', 'parent'), (('x', 'a'),)))
        for backend, store in self.stores():
            with self.subTest(backend=backend):
                store.apply(seed)
                self.rejected_without_changes(store, cycle)

    def test_scd_intersection_and_exclusive_boundary(self):
        first = Operation(
            'replace',
            'Node.period',
            ('valid_from', 'valid_to', 'value'),
            (('2026-01-01', '2026-01-03', 'b'),),
            (Condition('key', 'a'),),
        )
        seed = self.plan('seed', Operation('insert', 'Node', ('key',), (('a',), ('b',))), first)

        def update(start, end):
            return Operation(
                'replace',
                'Node.period',
                ('valid_from', 'valid_to', 'value'),
                ((start, end, 'a'),),
                (Condition('key', 'b'),),
            )

        separate = self.plan('separate', update('2026-01-03', '2026-01-05'))
        overlap = self.plan('overlap', update('2026-01-02', '2026-01-03'))
        for backend, store in self.stores():
            with self.subTest(backend=backend):
                store.apply(seed)
                self.apply_and_undo(store, separate)
                self.rejected_without_changes(store, overlap)

    def test_collection_member_change_creates_cycle(self):
        seed = self.plan(
            'seed',
            Operation(
                'insert', 'Node', ('key', 'children'), (('a', ('b',)), ('b', ('c',)), ('c', ()))
            ),
        )
        cycle = self.plan(
            'cycle',
            Operation(
                'replace', 'Node.children', ('value',), ((('a',),),), (Condition('key', 'b'),)
            ),
        )
        for backend, store in self.stores():
            with self.subTest(backend=backend):
                store.apply(seed)
                self.rejected_without_changes(store, cycle)

    def test_actual_subtype_controls_reference_activation(self):
        seed = self.plan(
            'seed',
            Operation('insert', 'Node', ('key', 'narrow'), (('a', 'a'),)),
            Operation('insert', 'Child', ('key',), (('b',),)),
        )
        dangling = self.plan(
            'dangling', Operation('update', 'Child', ('key', 'narrow'), (('b', 'a'),))
        )
        cycle = self.plan('cycle', Operation('update', 'Child', ('key', 'narrow'), (('b', 'b'),)))
        for backend, store in self.stores():
            with self.subTest(backend=backend):
                store.apply(seed)
                self.apply_and_undo(store, dangling)
                self.rejected_without_changes(store, cycle)
