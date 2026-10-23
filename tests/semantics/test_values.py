"""Structured values preserve references and reject recursive containment."""

import unittest
from dataclasses import replace

from tests.support import document
from yambl_compiler.semantics.validation import check


class ValueTests(unittest.TestCase):
    def test_imports_forward_references_and_generics(self):
        docs = [
            document(
                'namespace a from b import record entity Item { attr x: record attr all: values<record> attr indexed: map<string, record> }'
            ),
            document(
                "namespace b @title('Record') value record { @description('ref') attr item: Item static attr nested: leaf } value leaf { attr x: decimal[18, 4] } entity Item { id item_id: string }"
            ),
        ]
        self.assertEqual(check(docs), [])
        self.assertEqual(check(list(reversed(docs))), [])
        self.assertEqual(check([replace(d, prefix='fmc', repository='repo') for d in docs]), [])

    def test_invalid_value_contracts(self):
        cases = [
            ('value Bad {}', 'value names must use snake_case'),
            ('value a { id x: string }', 'value fields must be attr or static attr'),
            ('value a { config attr x: integer = 1 }', 'value fields must be attr or static attr'),
            (
                'value a { attr x: integer = 1 }',
                'defaults are currently supported only on config attr',
            ),
            (
                'value a { @dimension(x: string) attr x: string }',
                'value fields allow only title and description annotations',
            ),
            (
                'value a { @temporal(as_of_date: date) snapshot attr x: string }',
                'value fields must be attr or static attr',
            ),
            (
                'value a { @override attr x: string }',
                'value fields allow only title and description annotations',
            ),
            ('value a { attr x: string attr x: integer }', 'duplicate field: x'),
            ('value a { attr x: values<string> }', 'value fields cannot be collections'),
            (
                'value a {} entity A { attr x: collection<a> }',
                'collection requires an entity reference',
            ),
            (
                'value a {} entity A { attr x: type<a> }',
                'type requires Entity or a visible entity type',
            ),
            (
                'value a {} entity A { attr x: field_mapping<static, a> }',
                'field_mapping requires a visible entity type or self field target',
            ),
            ('value a {} extend a {}', 'extend requires a visible user-defined entity: a'),
            ('value a {} entity A inherits a {}', 'inherits requires a visible entity type: a'),
            ('value hierarchy_entry {}', 'duplicate or reserved declaration: hierarchy_entry'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertIn(expected, [d.message for d in check([document(source)])])

    def test_cycles_and_reference_boundaries(self):
        for source, count in [
            ('value a { attr x: a }', 1),
            ('value a { attr x: b } value b { attr x: a }', 2),
            (
                'value a { attr x: b attr y: c } value b { attr x: d } value c { attr x: d } value d {}',
                0,
            ),
            ('value a { attr x: EntityRef } entity EntityRef { attr x: a }', 0),
        ]:
            with self.subTest(source=source):
                self.assertEqual(
                    [d.message for d in check([document(source)])],
                    ['value containment cycle'] * count,
                )
