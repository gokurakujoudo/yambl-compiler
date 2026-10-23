"""External Decodes are value types with nonfatal, per-use diagnostics."""

import unittest
from pathlib import Path

from tests.support import document
from yambl_compiler.semantics.validation import check


class TypeTests(unittest.TestCase):
    def test_entity_types_and_arbitrary_self_fields(self):
        for mode in ('attr', 'static attr', 'config attr', 'snapshot attr', 'scd attr', 'id'):
            source = f'''entity Security {{ static attr currency: string }}
                entity Selection {{
                    attr conditions: field_mapping<static, self.target_kind>
                    {mode} target_kind: type<Security>
                }}
                entity Narrow inherits Selection {{ attr copied: field_mapping<attr, self.target_kind> }}
                extend Selection {{ attr extra: field_mapping<config, self.later> attr later: type<Entity> }}'''
            with self.subTest(mode=mode):
                self.assertEqual(check([document(source)]), [])
        self.assertEqual(
            check([document('''entity A {
            attr group: ConfigGroup
            attr types: values<type<Entity>>
            attr by_name: map<string, type<ConfigGroup>>
            attr a: field_mapping<attr, Decodes>
            attr b: field_mapping<snapshot, A>
            attr c: field_mapping<scd, A>
        }''')]),
            [],
        )

    def test_invalid_dependent_types(self):
        cases = [
            ('type', 'invalid type arguments for type'),
            ('type<string>', 'type requires Entity or a visible entity type'),
            ('type<Missing>', 'type requires Entity or a visible entity type'),
            ("type<'A'>", 'type requires Entity or a visible entity type'),
            ('type<A, A>', 'invalid type arguments for type'),
            ('type<type<A>>', 'type requires Entity or a visible entity type'),
            ('type<Codes>', 'type requires Entity or a visible entity type'),
            ('field_mapping', 'invalid type arguments for field_mapping'),
            ('field_mapping[1]', 'invalid type arguments for field_mapping'),
            (
                'field_mapping<id, A>',
                'field_mapping requires an attr, static, config, snapshot or scd filter',
            ),
            (
                "field_mapping<'static', A>",
                'field_mapping requires an attr, static, config, snapshot or scd filter',
            ),
            (
                'field_mapping<static<A>, A>',
                'field_mapping requires an attr, static, config, snapshot or scd filter',
            ),
            (
                'field_mapping<self.target, A>',
                'field_mapping requires an attr, static, config, snapshot or scd filter',
            ),
            (
                'field_mapping<static, Entity>',
                'field_mapping requires a visible entity type or self field target',
            ),
            (
                'field_mapping<static, Missing>',
                'field_mapping requires a visible entity type or self field target',
            ),
            (
                'field_mapping<static, Codes>',
                'field_mapping requires a visible entity type or self field target',
            ),
            ('field_mapping<static, self.absent>', 'unknown self field: absent'),
            ('field_mapping<static, self.bad>', 'self field must have a valid type<...> type: bad'),
            (
                'field_mapping<static, self.wrong>',
                'self field must have a valid type<...> type: wrong',
            ),
            (
                'field_mapping<static, self.many>',
                'self field must have a valid type<...> type: many',
            ),
            (
                'field_mapping<static, self.quoted>',
                'self field must have a valid type<...> type: quoted',
            ),
            ('values<self.target>', 'self fields are only valid as field_mapping targets'),
            ('code<self.target>', 'code requires a Decodes instance identifier'),
            ('code<Entity>', 'code requires a Decodes instance identifier'),
            ('collection<type<A>>', 'collection requires an entity reference'),
            ('values<field_mapping<static, A>>', 'values requires a value type'),
            ('map<string, field_mapping<static, A>>', 'map values cannot be collections'),
        ]
        for spelling, message in cases:
            with self.subTest(spelling=spelling):
                source = f'''decodes Codes {{}} entity A {{
                    attr target: type<Entity> attr bad: string attr wrong: type<Missing>
                    attr many: type<A, A> attr quoted: type<'A'> attr value: {spelling}
                }}'''
                self.assertIn(message, [d.message for d in check([document(source)])])

    def test_all_external_code_occurrences_are_reported(self):
        source = """entity Security {
    attr market: code<'TradingMarkets'>
    @dimension(source: code<PriceSources>)
    snapshot attr price: decimal
    attr markets: values<code<TradingMarkets>>
    attr by_name: map<string, code<'OtherSources'>>
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
            [(item.line, item.column) for item in diagnostics], [(2, 18), (3, 24), (5, 26), (6, 31)]
        )
        self.assertTrue(all(item.severity == 'warning' for item in diagnostics))
        self.assertTrue(all(item.path == Path('security.yambl') for item in diagnostics))
        self.assertIn(':2:18: warning: undefined Decodes: TradingMarkets', str(diagnostics[0]))

    def test_quoted_identifier_uses_scope_and_cannot_replace_reference(self):
        self.assertEqual(
            check([document("decodes Currencies {} entity A { attr c: code<'Currencies'> }")]), []
        )
        for expression in ["code<'A'>", 'code<Decodes>', 'code<integer>']:
            with self.subTest(expression=expression):
                diagnostics = check([document('entity A { attr x: ' + expression + ' }')])
                self.assertIn('Decodes instance identifier', diagnostics[0].message)
                self.assertEqual(diagnostics[0].severity, 'error')
        diagnostics = check([document("entity A { attr x: collection<'A'> }")])
        self.assertIn('only valid inside code', diagnostics[0].message)
        self.assertEqual(diagnostics[0].severity, 'error')
