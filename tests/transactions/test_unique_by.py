"""Check unique keys against both real backends and final transaction candidates."""

import unittest
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Success


class UniqueByTransactionTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    async def test_unique_key_spans_subtypes_and_checks_final_candidate(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """@unique_by(self.market, self.symbol)
entity Instrument { id id: string attr market: string attr symbol: string }
@unique_by(self.branch_key)
entity Bond inherits Instrument { @overload attr branch_key: string }
entity Fund inherits Instrument { @overload attr branch_key: string }
@unique_by(self.flag, self.count, self.label)
entity Tagged { id id: string attr flag: bool attr count: integer attr label: string }
@unique_by(self.primary)
entity Issuer { id id: string @reverse_relationship(Security.lead) attr primary: Security }
entity Security { id id: string @relationship(one, maybe) attr lead: Issuer }
@unique_by(self.issuer)
entity Holding { id id: string attr issuer: Issuer }
""",
        )
        model = compile_repository(self.root)
        output = self.root / 'generated'
        generate(model, output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')

        def plan(*operations):
            return Plan('unique', None, (Group('g', operations),))

        initial = plan(
            Operation('insert', 'Bond', ('id', 'market', 'symbol'), (('a', 'M', 'X'),)),
            Operation('insert', 'Fund', ('id', 'market', 'symbol'), (('b', 'M', 'Y'),)),
            Operation('insert', 'Bond', ('id', 'market'), (('c', 'M'),)),
        )
        conflict = plan(Operation('update', 'Fund', ('id', 'symbol'), (('b', 'X'),)))
        resolved = plan(
            Operation('update', 'Bond', ('id', 'symbol'), (('a', 'Y'),)),
            Operation('update', 'Fund', ('id', 'symbol'), (('b', 'Z'),)),
        )
        falsy_duplicate = plan(
            Operation(
                'insert',
                'Tagged',
                ('id', 'flag', 'count', 'label'),
                (('x', False, 0, ''), ('y', False, 0, '')),
            )
        )
        reference_duplicate = plan(
            Operation('insert', 'Issuer', ('id',), (('issuer',),)),
            Operation('insert', 'Holding', ('id', 'issuer'), (('h1', 'issuer'), ('h2', 'issuer'))),
        )
        reverse_good = plan(
            Operation('insert', 'Issuer', ('id',), (('i1',), ('i2',))),
            Operation('insert', 'Security', ('id', 'lead'), (('s', 'i1'),)),
        )
        subtree_valid = plan(
            Operation('update', 'Bond', ('id', 'branch_key'), (('a', 'same'),)),
            Operation('update', 'Fund', ('id', 'branch_key'), (('b', 'same'),)),
        )
        subtree_conflict = plan(Operation('update', 'Bond', ('id', 'branch_key'), (('c', 'same'),)))
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(initial).status, 'committed')
                    before = store.snapshot().tables
                    with self.assertRaisesRegex(ValueError, 'candidate entity validation failed'):
                        store.apply(conflict)
                    self.assertEqual(store.snapshot().tables, before)
                    for duplicate in (falsy_duplicate, reference_duplicate):
                        with self.assertRaisesRegex(
                            ValueError, 'candidate entity validation failed'
                        ):
                            store.apply(duplicate)
                        self.assertEqual(store.snapshot().tables, before)
                    self.assertEqual(store.apply(resolved).status, 'committed')
                    self.assertEqual(store.apply(subtree_valid).status, 'committed')
                    subtree_before = store.snapshot().tables
                    with self.assertRaisesRegex(ValueError, 'candidate entity validation failed'):
                        store.apply(subtree_conflict)
                    self.assertEqual(store.snapshot().tables, subtree_before)
                    self.assertEqual(store.apply(reverse_good).status, 'committed')
                    with closing(store.read()) as reader:
                        report = reader.validate()
                        self.assertIsInstance(report, Success)
                        self.assertTrue(report.value.valid, report.value.issues)
