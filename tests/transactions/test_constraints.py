"""Exercise constraint evaluation against both transaction candidates."""

import json
import os
import subprocess
import sys
import unittest
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.writing import dumps


class ConstraintTransactionTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    async def test_parent_constraint_dispatches_to_actual_subtype(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
entity A {
    id id: string
    computed attr amount: integer { return 1 }
    constraint positive { assert self.amount::value() > 0 }
}
entity B inherits A { @override computed attr amount: integer { return -1 } }
''',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        initial = Plan(
            'types', None, (Group('g', (Operation('insert', 'B', ('id',), (('b',),)),)),)
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    with self.assertRaises(ValueError) as caught:
                        store.apply(initial)
                    self.assertEqual(caught.exception.issues[0].entity, 'B')
                    self.assertEqual(
                        caught.exception.issues[0].constraint, 'A::constraint.positive'
                    )

    async def test_configuration_change_rechecks_untouched_instances(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            'entity A { id id: string config attr limit: integer = 1 constraint positive { assert self.limit::value() > 0 } }',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')

        def plan(*operations):
            return Plan('config', None, (Group('g', operations),))

        group = (Condition('config_group_id', 'bad'),)
        change = plan(
            Operation(
                'insert',
                'builtins.ConfigGroup',
                ('config_group_id', 'target_entity_type', 'priority'),
                (('bad', Symbol('A'), 10),),
            ),
            Operation(
                'replace', 'builtins.ConfigGroup.when', ('value',), ((ObjectValue(()),),), group
            ),
            Operation(
                'replace',
                'builtins.ConfigGroup.then_set',
                ('value',),
                ((ObjectValue(((Symbol('A.limit'), -1),)),),),
                group,
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(
                        store.apply(plan(Operation('insert', 'A', ('id',), (('a',),)))).status,
                        'committed',
                    )
                    before = store.snapshot().tables
                    with self.assertRaises(ValueError) as caught:
                        store.apply(change)
                    self.assertEqual(caught.exception.issues[0].instance_id, 'a')
                    self.assertEqual(store.snapshot().tables, before)

    async def test_records_states_and_recovery(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
entity A {
    id id: string
    snapshot attr price: integer
    constraint prices {
        let history = self.price::records()
        assert history.all(r -> r.value > 0)
        assert history.all(r -> r.value.recover(case EMPTY -> 1) > 0)
    }
}
''',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')

        def plan(*operations):
            return Plan('prices', None, (Group('g', operations),))

        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(
                        store.apply(plan(Operation('insert', 'A', ('id',), (('a',),)))).status,
                        'committed',
                    )
                    before = store.snapshot().tables
                    row = Operation(
                        'replace',
                        'A.price',
                        ('as_of_date', 'value'),
                        (('2026-01-01', Symbol('EMPTY')),),
                        (Condition('id', 'a'),),
                    )
                    with self.assertRaises(ValueError) as caught:
                        store.apply(plan(row))
                    self.assertEqual(
                        [(i.status, i.assert_source) for i in caught.exception.issues],
                        [('EMPTY', 'assert history.all(r -> r.value > 0)')],
                    )
                    self.assertEqual(
                        caught.exception.issues[0].dimensions, (('as_of_date', '2026-01-01'),)
                    )
                    self.assertEqual(store.snapshot().tables, before)
                    valid = Operation(
                        'replace',
                        'A.price',
                        ('as_of_date', 'value'),
                        (('2026-01-01', 4),),
                        (Condition('id', 'a'),),
                    )
                    self.assertEqual(store.apply(plan(valid)).status, 'committed')
                    invalid = Operation(
                        'replace',
                        'A.price',
                        ('as_of_date', 'value'),
                        (('2026-01-01', -1),),
                        (Condition('id', 'a'),),
                    )
                    with self.assertRaises(ValueError) as caught:
                        store.apply(plan(invalid))
                    self.assertTrue(
                        all(
                            issue.status == 'false'
                            and issue.dimensions == (('as_of_date', '2026-01-01'),)
                            for issue in caught.exception.issues
                        )
                    )
                    with closing(store.read()) as reader:
                        self.assertTrue(reader.validate().value.valid)
                source = self.write(f'{backend}-bad.yaplan', dumps(plan(row)))
                code = '''import sys
sys.path.insert(0, sys.argv[1])
from sample_model import SampleData
from sample_model.transactions.cli import main
raise SystemExit(main(SampleData, ['apply', '-o', 'config', sys.argv[2], '-o', 'source', sys.argv[3]]))
'''
                result = subprocess.run(
                    [
                        sys.executable,
                        '-I',
                        '-X',
                        'utf8',
                        '-O',
                        '-c',
                        code,
                        str(output),
                        str(config),
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    env={**os.environ, 'PYTHONUTF8': '1'},
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIsInstance(result.stderr, str)
                reports = [
                    json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')
                ]
                self.assertEqual(reports[0]['issues'][0]['status'], 'EMPTY')
                self.assertEqual(
                    reports[0]['issues'][0]['assert_source'], 'assert history.all(r -> r.value > 0)'
                )

    async def test_cross_reference_inheritance_and_final_candidate(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
entity Limit { id id: string attr amount: integer }
entity Account {
    id id: string
    attr target: Limit
    constraint positive {
        let amount = self.target.amount::value()
        assert amount > 0
        assert amount != -1
        let maximum = 100
        assert amount < maximum
    }
}
entity Child inherits Account { constraint own { assert self.target.amount::value() != 50 } }
''',
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')

        def plan(*operations):
            return Plan('constraints', None, (Group('g', operations),))

        initial = plan(
            Operation('insert', 'Limit', ('id', 'amount'), (('a', 1), ('b', 2))),
            Operation('insert', 'Child', ('id', 'target'), (('child', 'a'),)),
        )
        conflict = plan(Operation('update', 'Limit', ('id', 'amount'), (('a', -1),)))
        resolved = plan(
            Operation('update', 'Limit', ('id', 'amount'), (('a', -1),)),
            Operation('update', 'Account', ('id', 'target'), (('child', 'b'),)),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(initial).status, 'committed')
                    before = store.snapshot().tables
                    with self.assertRaisesRegex(
                        ValueError, 'candidate entity validation failed'
                    ) as caught:
                        store.apply(conflict)
                    self.assertIsInstance(caught.exception, module.EntityValidationError)
                    issues = caught.exception.issues
                    self.assertEqual(len(issues), 2)
                    self.assertEqual([item.status for item in issues], ['false', 'false'])
                    self.assertEqual(
                        [item.assert_source for item in issues],
                        ['assert amount > 0', 'assert amount != -1'],
                    )
                    self.assertTrue(
                        all(
                            item.constraint == 'Account::constraint.positive'
                            and item.entity == 'Child'
                            and item.instance_id == 'child'
                            and item.source_path == 'model.yambl'
                            and item.line > 0
                            for item in issues
                        )
                    )
                    self.assertEqual(store.snapshot().tables, before)
                    missing = plan(
                        Operation('delete', 'Limit', ('id',), (('a',),), owned_data=True)
                    )
                    with self.assertRaises(ValueError) as caught:
                        store.apply(missing)
                    self.assertTrue(
                        any(issue.status == 'ENTITY_NOT_FOUND' for issue in caught.exception.issues)
                    )
                    self.assertEqual(store.snapshot().tables, before)
                    self.assertEqual(store.apply(resolved).status, 'committed')
                    with closing(store.read()) as reader:
                        report = reader.validate()
                        self.assertTrue(report.value.valid, report)
                    self.assertEqual(
                        store.apply(
                            plan(Operation('update', 'Limit', ('id', 'amount'), (('a', 1),)))
                        ).status,
                        'committed',
                    )
                    moved = store.apply(
                        plan(Operation('update', 'Account', ('id', 'target'), (('child', 'a'),)))
                    )
                    self.assertEqual(moved.status, 'committed')
                    self.assertEqual(
                        store.apply(
                            plan(Operation('update', 'Limit', ('id', 'amount'), (('b', -2),)))
                        ).status,
                        'committed',
                    )
                    before_rollback = store.snapshot().tables
                    with self.assertRaisesRegex(ValueError, 'candidate entity validation failed'):
                        store.rollback(moved.path)
                    self.assertEqual(store.snapshot().tables, before_rollback)
