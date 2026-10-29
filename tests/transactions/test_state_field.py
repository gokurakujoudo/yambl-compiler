"""Exercise business field names that also name physical storage columns."""

import unittest
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.backends import validate_row
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.writing import dumps


class StateFieldTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''namespace shop
            decodes States { - 100 Active: 'Active' - 200 Inactive: 'Inactive' }
            entity Product { id key: string static attr state: code<States> }
            entity Note {
                id key: string
                static attr state: string
                static attr field: string
                @sparse('meta') static attr label: string
            }
            ''',
        )
        model = compile_repository(self.root)
        output = self.root / 'generated'
        self.layout = generate(model, output, 'state_model')
        self.api = load_generated(output / 'state_model', 'state_model')

    def plan(self, name, *operations):
        return self.write(name + '.yaplan', dumps(Plan(name, None, (Group('items', operations),))))

    async def test_state_payload_and_storage_states_on_both_backends(self):
        literals = ('VALUE', 'EMPTY', 'NO_ENTRY', 'NOT_APPLICABLE', 'custom')
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    backend + '.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await self.api.StateData.create(config) as store:
                    initial = self.plan(
                        'initial',
                        Operation(
                            'insert',
                            'shop.Product',
                            ('key', 'state'),
                            (
                                ('active', Symbol('shop.States.Active')),
                                ('inactive', Symbol('shop.States.Inactive')),
                            ),
                        ),
                        Operation(
                            'insert',
                            'shop.Note',
                            ('key', 'state', 'field', 'label'),
                            tuple(
                                (str(i), text, 'value-' + text, 'label')
                                for i, text in enumerate(literals)
                            ),
                        ),
                    )
                    store.apply(initial)
                    with closing(store.read()) as reader:
                        self.assertEqual(
                            str(reader.get_product('active').value.read_state().value), 'Active'
                        )
                        self.assertEqual(
                            str(reader.get_product('inactive').value.read_state().value), 'Inactive'
                        )
                        for i, text in enumerate(literals):
                            self.assertEqual(reader.get_note(str(i)).value.read_state().value, text)
                            self.assertEqual(
                                reader.get_note(str(i)).value.read_field().value, 'value-' + text
                            )
                            self.assertEqual(
                                reader.get_note(str(i)).value.read_label().value, 'label'
                            )
                        self.assertTrue(reader.validate().value.valid)
                    table = next(t for t in self.layout.tables if t.name == 'tb_shop_product')
                    row = next(
                        r for r in store.snapshot().tables[table.name] if r['id'] == 'active'
                    )
                    validate_row(self.layout, table, row)
                    with self.assertRaisesRegex(ValueError, 'invalid stored field state'):
                        validate_row(self.layout, table, {**row, 'state__state': 99})
                    sparse_table = next(
                        t
                        for t in self.layout.tables
                        if t.name == self.layout.fields['shop.Note.label'].table
                    )
                    sparse_row = store.snapshot().tables[sparse_table.name][0]
                    validate_row(self.layout, sparse_table, sparse_row)
                    with self.assertRaisesRegex(ValueError, 'unknown logical field'):
                        validate_row(self.layout, sparse_table, {**sparse_row, 'field': 'unknown'})
                    for state in ('EMPTY', 'NOT_APPLICABLE', 'NO_ENTRY'):
                        change = self.plan(
                            'change_' + state.lower(),
                            Operation(
                                'update',
                                'shop.Product',
                                ('key', 'state'),
                                (('active', Symbol(state)),),
                            ),
                        )
                        transaction = store.apply(change)
                        with closing(store.read()) as reader:
                            self.assertEqual(
                                reader.get_product('active').value.read_state().status.value, state
                            )
                            self.assertTrue(reader.validate().value.valid)
                        store.rollback(transaction.path)
                        with closing(store.read()) as reader:
                            self.assertEqual(
                                str(reader.get_product('active').value.read_state().value), 'Active'
                            )
