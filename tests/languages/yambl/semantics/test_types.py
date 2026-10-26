"""External Decodes are value types with nonfatal, per-use diagnostics."""

import unittest
from pathlib import Path

from tests.support import document
from yambl_compiler.languages.yambl.semantics.validation import check


class TypeTests(unittest.TestCase):
    def test_entity_types_and_arbitrary_self_fields(self):
        for mode in (
            'attr',
            'static attr',
            'config attr',
            'snapshot attr',
            'scd attr',
            'id',
        ):
            source = f"""entity Security {{ static attr currency: string }}
                entity Selection {{
                    attr conditions: field_mapping<AttrCats.Static, self.target_kind::value>
                    {mode} target_kind: type<Security>
                }}
                entity Narrow inherits Selection {{ attr copied: field_mapping<AttrCats.Static, self.target_kind::value> }}
                extend Selection {{ attr extra: field_mapping<AttrCats.Config, self.later::value> attr later: type<Entity> }}"""
            with self.subTest(mode=mode):
                self.assertEqual(check([document(source)]), [])
        self.assertEqual(
            check([document('''entity A {
            attr group: ConfigGroup
            attr types: values<type<Entity>>
            attr by_name: mapping<string, type<ConfigGroup>>
            attr a: field_mapping<AttrCats.Static, Decodes>
            attr b: field_mapping<AttrCats.Snapshot, A>
            attr c: field_mapping<AttrCats.Scd, A>
        }''')]),
            [],
        )

    def test_invalid_dependent_types(self):
        cases = [
            ('type', 'invalid type arguments for type'),
            ('type<string>', 'type requires Entity or a visible entity type'),
            ('type<Missing>', 'unknown type: Missing'),
            ("type<'A'>", 'type position requires a type or an entity type value'),
            ('type<A, A>', 'invalid type arguments for type'),
            ('type<type<A>>', 'type requires Entity or a visible entity type'),
            ('type<Codes>', 'type position requires a type or an entity type value'),
            ('field_mapping', 'invalid type arguments for field_mapping'),
            ('field_mapping[1]', 'invalid type arguments for field_mapping'),
            ('field_mapping<id, A>', 'incompatible argument for field_mapping.a'),
            ("field_mapping<'static', A>", 'incompatible argument for field_mapping.a'),
            ('field_mapping<static<A>, A>', 'static does not accept generic arguments'),
            (
                'field_mapping<self.target::value, A>',
                'incompatible argument for field_mapping.a',
            ),
            (
                'field_mapping<AttrCats.Static, Missing>',
                'incompatible argument for field_mapping.b',
            ),
            (
                'field_mapping<AttrCats.Static, Codes>',
                'incompatible argument for field_mapping.b',
            ),
            (
                'field_mapping<AttrCats.Static, self.absent::value>',
                'unknown field: absent',
            ),
            (
                'field_mapping<AttrCats.Static, self.bad::value>',
                'incompatible argument for field_mapping.b',
            ),
            ('values<self.target::value>', 'values requires a value type'),
            ('code<self.target::value>', 'code requires a Decodes instance value'),
            ('code<Entity>', 'code requires a Decodes instance identifier'),
            ('collection<type<A>>', 'collection requires an entity reference'),
            (
                'values<field_mapping<AttrCats.Static, A>>',
                'values requires a value type',
            ),
            (
                'mapping<string, field_mapping<AttrCats.Static, A>>',
                'mapping values cannot be collections',
            ),
        ]
        for spelling, message in cases:
            with self.subTest(spelling=spelling):
                source = f"decodes Codes {{}} entity A {{ attr target: type<Entity> attr bad: string attr item: {spelling} }}"
                self.assertIn(message, [d.message for d in check([document(source)])])
        self.assertEqual(
            check([document('entity A { attr x: field_mapping<AttrCats.Static, Entity> }')]),
            [],
        )

    def test_unresolved_codes_distinguish_names_from_external_identifiers(self):
        source = """entity Security {
    attr market: code<'TradingMarkets'>
    dimension price_source { value source: code<PriceSources> }
    @dimension(price_source)
    snapshot attr price: decimal
    attr markets: values<code<TradingMarkets>>
    attr by_name: mapping<string, code<'OtherSources'>>
}
"""
        diagnostics = check([document(source, 'security.yambl')])
        self.assertEqual(
            [item.message for item in diagnostics],
            [
                'undefined Decodes: TradingMarkets',
                'undefined Decodes: PriceSources',
                'undefined Decodes: TradingMarkets',
                'undefined Decodes: OtherSources',
            ],
        )
        self.assertEqual(
            [(item.line, item.column) for item in diagnostics],
            [(2, 18), (3, 49), (6, 31), (7, 35)],
        )
        self.assertEqual(
            [item.severity for item in diagnostics],
            ['warning', 'error', 'error', 'warning'],
        )
        self.assertTrue(all(item.path == Path('security.yambl') for item in diagnostics))
        self.assertIn(':2:18: warning: undefined Decodes: TradingMarkets', str(diagnostics[0]))

    def test_quoted_identifier_uses_scope_and_cannot_replace_reference(self):
        self.assertEqual(
            check([document("decodes Currencies {} entity A { attr c: code<'Currencies'> }")]),
            [],
        )
        for expression in ["code<'A'>", 'code<Decodes>', 'code<integer>']:
            with self.subTest(expression=expression):
                diagnostics = check([document('entity A { attr x: ' + expression + ' }')])
                self.assertIn('Decodes instance identifier', diagnostics[0].message)
                self.assertEqual(diagnostics[0].severity, 'error')
        diagnostics = check([document("entity A { attr x: collection<'A'> }")])
        self.assertIn('type position requires', diagnostics[0].message)
        self.assertEqual(diagnostics[0].severity, 'error')
