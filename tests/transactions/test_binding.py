"""Verify business field and literal binding against compiled model contracts."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from tests.languages.support import source
from tests.storage.test_runtime import SOURCE
from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import ObjectValue, Symbol
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.transactions.binding import Catalog, object_items
from yambl_compiler.transactions.syntax import parse


class CatalogCase(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        self.catalog = Catalog(build_layout(compile_repository(self.root)))
        self.entity = self.catalog.entity('A')


class BindingTests(CatalogCase):
    def test_shared_editor_type_cases(self):
        fixture = (
            Path(__file__).resolve().parents[3]
            / 'yambl/tools/vscode/lsp/tests/transaction-type-cases.json'
        )
        data = json.loads(fixture.read_text(encoding='utf-8'))
        self.write('model.yambl', data['model'])
        catalog = Catalog(build_layout(compile_repository(self.root)))
        for case in data['cases']:
            with self.subTest(case=case):
                entity = catalog.entity(case['entity'])
                field = catalog.field(entity, case['field'])[0]
                value = (
                    parse(source('insert core.Item (id) {\n' + case['literal'] + '\n}'))
                    .groups[0]
                    .operations[0]
                    .rows[0][0]
                )
                if case['valid']:
                    catalog.scalar(field.type, value)
                else:
                    with self.assertRaises(ValueError):
                        catalog.scalar(field.type, value)

    def test_entity_and_field_resolution(self):
        child = self.catalog.entity('C')
        self.assertEqual((child.root, child.column, child.id.name), ('A', 'id', 'key'))
        field, stored = self.catalog.field(child, 'limit')
        self.assertEqual(field.default.text, '30')
        self.assertEqual(stored.field.identity, 'A.limit')
        self.assertEqual(self.catalog.target('C.limit'), (child, field, stored))
        self.assertEqual(self.catalog.target('A'), (self.entity, None, None))
        builtin = self.catalog.entity('builtins.Hierarchy')
        self.assertEqual(builtin.column, 'hierarchy_id')
        self.assertIn('hierarchy_entries', self.catalog.fields(builtin))
        for name in ('missing', 'Currencies', 'builtins.Entity'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.catalog.entity(name)
        for name in ('unknown', 'key'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.catalog.field(self.entity, name)

    def test_missing_storage_bindings_are_not_silently_accepted(self):
        layout = self.catalog.layout
        without_entity = Catalog(
            replace(layout, roots={k: v for k, v in layout.roots.items() if k != 'A'})
        )
        with self.assertRaisesRegex(ValueError, 'entity has no storage: A'):
            without_entity.entity('A')
        without_field = Catalog(
            replace(layout, fields={k: v for k, v in layout.fields.items() if k != 'A.text'})
        )
        with self.assertRaisesRegex(ValueError, 'field has no storage: A.text'):
            without_field.field(self.entity, 'text')

    def test_scalar_encoding_and_decoding(self):
        cases = [
            ('text', 'word', 'word'),
            ('flag', True, 1),
            ('count', 12, '12'),
            ('day', '2026-10-26', '2026-10-26'),
            ('amount', Decimal('12.500'), '12.5'),
            ('amount', 12, '12'),
            ('amount', Decimal('-0.0'), '0'),
            ('currency', Symbol('Currencies.CNY'), 'CNY'),
            ('currency', 'USD', 'USD'),
            ('other', 42, '42'),
        ]
        for name, value, expected in cases:
            field, _ = self.catalog.field(self.entity, name)
            with self.subTest(name=name, value=value):
                encoded = self.catalog.scalar(field.type, value)
                self.assertEqual(encoded, expected)
                decoded = self.catalog.literal(field.type, encoded)
                self.assertEqual(self.catalog.scalar(field.type, decoded), expected)
        for name, value in [
            ('text', 1),
            ('flag', 1),
            ('count', True),
            ('day', '2026-1-1'),
            ('day', 1),
            ('amount', True),
            ('amount', Decimal('NaN')),
            ('amount', Decimal('10000')),
            ('amount', Decimal('0.001')),
            ('currency', Symbol('Other.CNY')),
            ('currency', 1),
            ('currency', 'Missing'),
            ('other', '42'),
            ('members', (1,)),
        ]:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                self.catalog.scalar(self.catalog.field(self.entity, name)[0].type, value)
        for name, payload in [('flag', True), ('flag', 2), ('count', '01'), ('text', 1)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.catalog.literal(self.catalog.field(self.entity, name)[0].type, payload)

    def test_type_names_and_dynamic_codes(self):
        config = self.catalog.entity('builtins.ConfigGroup')
        type_ = self.catalog.field(config, 'entity_type')[0].type
        self.assertEqual(self.catalog.scalar(type_, Symbol('B')), 'B')
        self.assertEqual(self.catalog.literal(type_, 'A'), Symbol('A'))
        with self.assertRaisesRegex(ValueError, 'type name'):
            self.catalog.scalar(type_, 'A')
        bound = replace(type_, arguments=(replace(type_.arguments[0], target='B'),))
        self.assertEqual(self.catalog.scalar(bound, Symbol('B')), 'B')
        with self.assertRaisesRegex(ValueError, 'derive'):
            self.catalog.scalar(bound, Symbol('Other'))
        stored = self.catalog.field(self.entity, 'price')[1]
        self.assertEqual(self.catalog.scalar(stored.dimensions[0].type, 'AnyVendor'), 'AnyVendor')

    def test_unique_object_members(self):
        self.assertEqual(
            object_items(ObjectValue(((Symbol('a'), 1), ('b', False)))), {'a': 1, 'b': False}
        )
        for value in ('bad', ObjectValue(((1, 2),)), ObjectValue(((Symbol('a'), 1), ('a', 2)))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                object_items(value)
