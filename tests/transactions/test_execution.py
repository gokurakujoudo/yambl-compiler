"""Check candidate mutation and conflict-aware rollback on real compiled layouts."""

from dataclasses import replace
from decimal import Decimal

from tests.transactions.test_binding import CatalogCase
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution, rollback
from yambl_compiler.transactions.model import (
    Condition,
    Group,
    Image,
    Operation,
    Plan,
    Symbol,
    Transaction,
)
from yambl_compiler.transactions.writing import seal


class ExecutionTests(CatalogCase):
    def setUp(self):
        super().setUp()
        self.data = Data(
            self.catalog.layout,
            {
                t.name: [dict(zip((c.name for c in t.columns), row, strict=True)) for row in t.rows]
                for t in self.catalog.layout.tables
            },
        )
        self.data.insert(self.entity, 'a')
        self.scope = (Condition('key', 'a'),)

    def apply(self, *groups):
        plan = Plan('test', self.catalog.layout.model.model_digest, groups)
        results = Execution(self.data).apply(plan)
        return seal(
            Transaction(
                '12345678-1234-1234-1234-123456789abc', '2026-10-26T00:00:00Z', plan, results
            )
        )

    def update(self, name, value, group='main'):
        return self.apply(Group(group, (Operation('update', 'A', ('key', name), (('a', value),)),)))

    def put(self, target, rows, columns=('value',), scope=None):
        selected = self.scope if scope is None else scope
        self.data.put(target, selected, Image(target, columns, rows))

    def test_patch_rollback_preserves_later_fields(self):
        self.put('A.text', (('before',),))
        transaction = self.update('text', 'after')
        self.put('A.count', ((42,),))
        results = rollback(self.data, transaction)
        self.assertEqual(self.data.image('A.text', self.scope).rows, (('a', 'before'),))
        self.assertEqual(self.data.image('A.count', self.scope).rows, (('a', 42),))
        self.assertEqual(results[0].warnings, ())
        reverse = seal(replace(transaction, groups=results, source=transaction.id))
        rollback(self.data, reverse)
        self.assertEqual(self.data.image('A.text', self.scope).rows, (('a', 'after'),))
        self.put('A.text', (('later',),))
        result = rollback(self.data, transaction)
        self.assertEqual(len(result[0].warnings), 1)
        self.assertEqual(result[0].changes, ())
        self.assertEqual(self.data.image('A.text', self.scope).rows, (('a', 'later'),))

    def test_scd_split_and_outside_changes_survive_rollback(self):
        columns = ('valid_from', 'valid_to', 'value')
        self.put('A.outstanding', (('2026-01-01', '2026-02-01', Decimal('100')),), columns)
        scope = (*self.scope, Condition('day', '2026-01-10', '2026-01-20'))
        operation = Operation(
            'replace',
            'A.outstanding',
            columns,
            (('2026-01-10', '2026-01-20', Decimal('200')),),
            scope,
        )
        transaction = self.apply(Group('period', (operation,)))
        self.assertEqual(
            self.data.image('A.outstanding', self.scope).rows,
            (
                ('a', '2026-01-01', '2026-01-10', Decimal('100')),
                ('a', '2026-01-10', '2026-01-20', Decimal('200')),
                ('a', '2026-01-20', '2026-02-01', Decimal('100')),
            ),
        )
        self.put(
            'A.outstanding',
            (('2026-01-20', '2026-02-01', Decimal('300')),),
            columns,
            (*self.scope, Condition('day', '2026-01-20', '2026-02-01')),
        )
        # Equivalent subdivision inside the scope must not cause a conflict.
        self.put(
            'A.outstanding',
            (
                ('2026-01-10', '2026-01-15', Decimal('200')),
                ('2026-01-15', '2026-01-20', Decimal('200')),
            ),
            columns,
            scope,
        )
        result = rollback(self.data, transaction)
        self.assertEqual(result[0].warnings, ())
        self.assertEqual(
            self.data.image('A.outstanding', self.scope).rows[-1],
            ('a', '2026-01-20', '2026-02-01', Decimal('300')),
        )
        self.assertEqual(
            self.data.image('A.outstanding', scope).rows,
            (('a', '2026-01-10', '2026-01-20', Decimal('100')),),
        )

    def test_group_selection_and_whole_scope_conflict(self):
        scope = (*self.scope, Condition('source', 'Vendor'))
        first = Operation(
            'replace',
            'A.price',
            ('day', 'value'),
            (('2026-01-01', Decimal('1')), ('2026-01-02', Decimal('2'))),
            scope,
        )
        second = Operation('update', 'A', ('key', 'text'), (('a', 'changed'),))
        transaction = self.apply(Group('prices', (first,)), Group('labels', (second,)))
        self.put(
            'A.price',
            (('2026-01-01', Decimal('3')), ('2026-01-02', Decimal('2'))),
            ('day', 'value'),
            scope,
        )
        results = rollback(self.data, transaction, ('prices',))
        self.assertEqual([g.name for g in results], ['prices'])
        self.assertEqual(results[0].changes, ())
        self.assertEqual(len(results[0].warnings), 1)
        self.assertEqual(self.data.image('A.text', self.scope).rows, (('a', 'changed'),))
        with self.assertRaisesRegex(ValueError, 'group'):
            rollback(self.data, transaction, ('missing',))

    def test_entity_delete_restore_and_insert_conflicts(self):
        self.put('A.text', (('owned',),))
        with self.assertRaisesRegex(ValueError, 'owned_data'):
            self.data.delete(self.entity, 'a', False)
        transaction = self.apply(
            Group('remove', (Operation('delete', 'A', ('key',), (('a',),), owned_data=True),))
        )
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.data.actual(self.entity, 'a')
        rollback(self.data, transaction)
        self.assertEqual(self.data.image('A.text', self.scope).rows, (('a', 'owned'),))
        result = rollback(self.data, transaction)
        self.assertEqual(len(result[0].warnings), 1)
        inserted = self.apply(
            Group('new', (Operation('insert', 'B', ('key', 'extra'), (('b', 'new'),)),))
        )
        self.data.put(
            'B.text', (Condition('key', 'b'),), Image('B.text', ('value',), (('later',),))
        )
        self.assertEqual(len(rollback(self.data, inserted)[0].warnings), 1)

    def test_collections_states_and_conflicting_operations(self):
        self.data.insert(self.catalog.entity('Other'), 1)
        self.put('A.members', (((1,),),))
        self.assertEqual(self.data.image('A.members', self.scope).rows, (('a', (1,)),))
        self.put('A.members', (((),),))
        self.assertEqual(self.data.image('A.members', self.scope).rows, (('a', ()),))
        self.put('A.members', ((Symbol('EMPTY'),),))
        self.assertEqual(self.data.image('A.members', self.scope).rows, (('a', Symbol('EMPTY')),))
        repeated = Operation('update', 'A', ('key', 'text'), (('a', 'same'),))
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            self.apply(Group('one', (repeated,)), Group('two', (repeated,)))
