"""Check candidate mutation and conflict-aware rollback on real compiled layouts."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

from tests.transactions.test_binding import CatalogCase
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.languages.yatran.model import Image, Transaction
from yambl_compiler.languages.yatran.writing import seal
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution, rollback, same


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

    def test_candidate_field_entry_points_reject_entity_targets(self):
        for operation in (
            lambda: self.data.image('A', self.scope),
            lambda: self.data.clear('A', self.scope),
            lambda: self.data.put('A', self.scope, Image('A', (), ())),
            lambda: Execution(self.data).field(Operation('replace', 'A', (), (), self.scope)),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'field'):
                operation()
        with self.assertRaisesRegex(ValueError, 'scope requires owner ID'):
            self.data.image('A.text', ())
        with self.assertRaisesRegex(ValueError, 'replace or delete'):
            Execution(self.data).field(Operation('update', 'A.text', (), (), self.scope))
        with self.assertRaisesRegex(ValueError, 'invalid entity operation'):
            Execution(self.data).entities(Operation('replace', 'A', ('key',), (('a',),)))
        self.assertEqual(self.data.image('A.text', self.scope).rows, (('a', Symbol('NO_ENTRY')),))

    def test_whole_entity_and_wider_scopes_conflict_with_prior_writes(self):
        cases = (
            (
                Operation('update', 'A', ('key', 'text'), (('a', 'first'),)),
                Operation('delete', 'A', ('key',), (('a',),), owned_data=True),
            ),
            (
                Operation('replace', 'A.price', ('day', 'source', 'value'), (), self.scope),
                Operation('delete', 'A.price', (), (), (*self.scope, Condition('source', 'One'))),
            ),
            (
                Operation('delete', 'A.price', (), (), (*self.scope, Condition('source', 'One'))),
                Operation('replace', 'A.price', ('day', 'source', 'value'), (), self.scope),
            ),
        )
        before = deepcopy(self.data.tables)
        for operations in cases:
            with self.subTest(operations=operations):
                candidate = Data(self.data.layout, deepcopy(before))
                with self.assertRaisesRegex(ValueError, 'overlapping operations'):
                    Execution(candidate).apply(
                        Plan(
                            'overlap',
                            self.data.layout.model.model_digest,
                            (Group('all', operations),),
                        )
                    )
        self.assertEqual(self.data.tables, before)

    def test_corrupt_payloads_and_missing_owners_are_rejected(self):
        _, stored = self.catalog.field(self.entity, 'text')
        row = self.data.tables[stored.table][0]
        row[stored.state] = 'UNSUPPORTED'
        with self.assertRaisesRegex(ValueError, 'invalid stored state'):
            self.data.image('A.text', self.scope)
        row[stored.state] = 'NO_ENTRY'
        self.put('A.members', (((),),))
        member_table = self.catalog.field(self.entity, 'members')[1].members
        self.data.tables[member_table].append({'id': 'missing', 'member_id': '1'})
        with self.assertRaisesRegex(ValueError, 'missing owner'):
            self.data.validate()
        self.data.tables[member_table].clear()

        for name in ('first', 'second'):
            scope = (Condition('config_group_id', name),)
            self.data.insert(self.catalog.entity('builtins.ConfigGroup'), name)
            for field, value in (
                ('entity_type', Symbol('A')),
                ('priority', 1),
                ('when', ObjectValue(())),
                ('then_set', ObjectValue(((Symbol('A.peers'), ()),))),
            ):
                self.put('builtins.ConfigGroup.' + field, ((value,),), scope=scope)
        self.data.validate()
        table = self.data.tables['tb_config_group_then_set']
        original = table[0]['value']
        for content, message in (
            ('[]', 'mapping payload'),
            ('{}', 'mapping payload'),
            ('{"value": 1}', 'configuration collection'),
        ):
            table[0]['value'] = content
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, message):
                self.data.image(
                    'builtins.ConfigGroup.then_set', (Condition('config_group_id', 'first'),)
                )
        table[0]['value'] = original
        self.data.validate()

    def test_rollback_rejects_missing_field_before_image(self):
        transaction = self.update('text', 'after')
        group = transaction.groups[0]
        malformed = replace(
            transaction, groups=(replace(group, changes=(replace(group.changes[0], before=()),)),)
        )
        before = deepcopy(self.data.tables)
        with self.assertRaisesRegex(ValueError, 'one before image'):
            rollback(self.data, malformed)
        self.assertEqual(self.data.tables, before)

    def test_config_group_entity_type_is_a_business_attribute(self):
        scope = (Condition("config_group_id", "group"),)
        transaction = self.apply(
            Group(
                "configuration",
                (
                    Operation(
                        "insert",
                        "builtins.ConfigGroup",
                        ("config_group_id", "entity_type", "priority"),
                        (("group", Symbol("A"), 10),),
                    ),
                    Operation(
                        "replace",
                        "builtins.ConfigGroup.when",
                        ("value",),
                        ((ObjectValue(()),),),
                        scope,
                    ),
                    Operation(
                        "replace",
                        "builtins.ConfigGroup.then_set",
                        ("value",),
                        ((ObjectValue(((Symbol("A.limit"), 7),)),),),
                        scope,
                    ),
                ),
            )
        )
        entity = self.catalog.entity("builtins.ConfigGroup")
        self.assertEqual(self.data.actual(entity, "group"), entity)
        original = self.data.entity_images(entity, "group")
        self.assertEqual(
            self.data.image("builtins.ConfigGroup.entity_type", scope).rows,
            (("group", Symbol("A")),),
        )
        reversed_groups = rollback(self.data, transaction)
        self.assertEqual(self.data.tables["tb_config_group"], [])
        rollback(self.data, replace(transaction, groups=reversed_groups))
        self.assertEqual(self.data.entity_images(entity, "group"), original)

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

    def test_config_collection_order_is_not_a_rollback_conflict(self):
        scope = (Condition('config_group_id', 'ordered'),)
        for id_ in (1, 2):
            self.data.insert(self.catalog.entity('Other'), id_)
        self.apply(
            Group(
                'initial',
                (
                    Operation(
                        'insert',
                        'builtins.ConfigGroup',
                        ('config_group_id', 'entity_type', 'priority'),
                        (('ordered', Symbol('A'), 1),),
                    ),
                    Operation(
                        'replace',
                        'builtins.ConfigGroup.when',
                        ('value',),
                        ((ObjectValue(()),),),
                        scope,
                    ),
                    Operation(
                        'replace',
                        'builtins.ConfigGroup.then_set',
                        ('value',),
                        ((ObjectValue(()),),),
                        scope,
                    ),
                ),
            )
        )
        target = 'builtins.ConfigGroup.then_set'
        transaction = self.apply(
            Group(
                'peers',
                (
                    Operation(
                        'replace',
                        target,
                        ('value',),
                        ((ObjectValue(((Symbol('A.peers'), (1, 2)),)),),),
                        scope,
                    ),
                ),
            )
        )
        self.data.put(
            target,
            scope,
            Image(target, ('value',), ((ObjectValue(((Symbol('A.peers'), (2, 1)),)),),)),
        )
        reverse = rollback(self.data, transaction)
        self.assertEqual(reverse[0].warnings, ())
        self.assertEqual(self.data.image(target, scope).rows, (('ordered', ObjectValue(())),))
        redone = rollback(self.data, replace(transaction, groups=reverse))
        self.assertEqual(redone[0].warnings, ())
        self.assertEqual(set(self.data.image(target, scope).rows[0][1].items[0][1]), {1, 2})

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

    def test_scd_collection_delete_preserves_outside_members_and_redo(self):
        for id_ in (1, 2, 3):
            self.data.insert(self.catalog.entity('Other'), id_)
        target = 'A.owners'
        columns = ('valid_from', 'valid_to', 'value')
        self.put(target, (('2026-01-01', '2026-02-01', (1, 2)),), columns)
        scope = (*self.scope, Condition('day', '2026-01-10', '2026-01-20'))
        transaction = self.apply(Group('remove', (Operation('delete', target, (), (), scope),)))
        self.assertEqual(
            self.data.image(target, self.scope).rows,
            (
                ('a', '2026-01-01', '2026-01-10', (1, 2)),
                ('a', '2026-01-20', '2026-02-01', (1, 2)),
            ),
        )
        self.assertEqual(self.data.image(target, scope).rows, ())
        self.put(
            target,
            (('2026-01-20', '2026-02-01', (3,)),),
            columns,
            (*self.scope, Condition('day', '2026-01-20', '2026-02-01')),
        )
        reversed_groups = rollback(self.data, transaction)
        self.assertEqual(reversed_groups[0].warnings, ())
        restored = self.data.image(target, self.scope)
        self.assertTrue(
            same(
                (restored,),
                (
                    Image(
                        target,
                        restored.columns,
                        (
                            ('a', '2026-01-01', '2026-01-20', (2, 1)),
                            ('a', '2026-01-20', '2026-02-01', (3,)),
                        ),
                    ),
                ),
            )
        )
        redone = rollback(self.data, replace(transaction, groups=reversed_groups))
        self.assertEqual(redone[0].warnings, ())
        self.assertEqual(self.data.image(target, scope).rows, ())
        self.assertEqual(
            self.data.image(target, self.scope).rows,
            (
                ('a', '2026-01-01', '2026-01-10', (1, 2)),
                ('a', '2026-01-20', '2026-02-01', (3,)),
            ),
        )

    def test_delete_entity_without_owned_data_and_restore(self):
        self.data.insert(self.catalog.entity('B'), 'bare')
        original = self.data.entity_images(self.catalog.entity('B'), 'bare')
        transaction = self.apply(
            Group('remove', (Operation('delete', 'B', ('key',), (('bare',),)),))
        )
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.data.actual(self.entity, 'bare')
        result = rollback(self.data, transaction)
        self.assertEqual(result[0].warnings, ())
        self.assertEqual(self.data.entity_images(self.catalog.entity('B'), 'bare'), original)

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

    def test_unordered_typed_images_preserve_real_differences(self):
        def image(value):
            return (Image('A.value', ('key', 'value'), (('a', value),)),)

        left = ObjectValue(((Symbol('first'), (1, 2)), (Symbol('second'), Decimal('1.00'))))
        right = ObjectValue((('second', 1), ('first', (2, 1))))
        self.assertTrue(same(image(left), image(right)))
        self.assertFalse(same(image((1, 2)), image((1, 3))))
        self.assertFalse(same(image(True), image(1)))
        self.assertFalse(same(image(Symbol('EMPTY')), image('EMPTY')))
        self.assertFalse(same(image(()), image(Symbol('NO_ENTRY'))))

    def test_partial_field_conflict_does_not_block_other_field_rollback(self):
        self.put('A.text', (('original',),))
        self.put('A.count', ((1,),))
        transaction = self.apply(
            Group(
                'patch',
                (Operation('update', 'A', ('key', 'text', 'count'), (('a', 'updated', 2),)),),
            )
        )
        self.put('A.text', (('later',),))
        result = rollback(self.data, transaction)
        self.assertEqual(len(result[0].warnings), 1)
        self.assertEqual([c.target for c in result[0].changes], ['A.count'])
        self.assertEqual(self.data.image('A.text', self.scope).rows, (('a', 'later'),))
        self.assertEqual(self.data.image('A.count', self.scope).rows, (('a', 1),))

    def test_later_entity_replacement_and_missing_entity_are_preserved(self):
        inserted = self.apply(
            Group('new', (Operation('insert', 'B', ('key', 'extra'), (('b', 'new'),)),))
        )
        self.data.delete(self.catalog.entity('B'), 'b', True)
        self.data.insert(self.catalog.entity('C'), 'b')
        result = rollback(self.data, inserted)
        self.assertEqual(result[0].changes, ())
        self.assertIn('later entity type retained', result[0].warnings[0])
        self.assertEqual(self.data.actual(self.entity, 'b').definition.identity, 'C')
        changed = self.update('text', 'updated')
        self.data.delete(self.entity, 'a', True)
        result = rollback(self.data, changed)
        self.assertEqual(result[0].changes, ())
        self.assertIn('missing data retained', result[0].warnings[0])
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.data.actual(self.entity, 'a')

    def test_disjoint_scopes_and_new_entity_owned_data_share_one_plan(self):
        first = (
            *self.scope,
            Condition('source', 'One'),
            Condition('day', '2026-01-01', '2026-02-01'),
        )
        second = (
            *self.scope,
            Condition('source', 'One'),
            Condition('day', '2026-02-01', '2026-03-01'),
        )
        third = (*self.scope, Condition('source', 'Two'))
        transaction = self.apply(
            Group(
                'disjoint',
                (
                    Operation(
                        'replace',
                        'A.price',
                        ('day', 'value'),
                        (('2026-01-10', Decimal('1')),),
                        first,
                    ),
                    Operation(
                        'replace',
                        'A.price',
                        ('day', 'value'),
                        (('2026-02-10', Decimal('2')),),
                        second,
                    ),
                    Operation(
                        'replace',
                        'A.price',
                        ('day', 'value'),
                        (('2026-01-10', Decimal('3')),),
                        third,
                    ),
                    Operation('insert', 'B', ('key',), (('b',),)),
                    Operation(
                        'replace', 'B.extra', ('value',), (('owned',),), (Condition('key', 'b'),)
                    ),
                ),
            )
        )
        self.assertEqual(len(self.data.image('A.price', self.scope).rows), 3)
        self.assertEqual(
            self.data.image('B.extra', (Condition('key', 'b'),)).rows, (('b', 'owned'),)
        )
        result = rollback(self.data, transaction)
        self.assertEqual(result[0].warnings, ())
        self.assertEqual(self.data.image('A.price', self.scope).rows, ())
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.data.actual(self.entity, 'b')
