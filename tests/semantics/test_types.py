"""External Decodes are value types with nonfatal, per-use diagnostics."""

import unittest
from pathlib import Path

from tests.support import document
from yambl_compiler.semantics.validation import check


class TypeTests(unittest.TestCase):
    def test_all_external_code_occurrences_are_reported(self):
        source = """entity Security {
    attr market: code<'TradingMarkets'>
    @dimensions(source: code<PriceSources>)
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
            [(item.line, item.column) for item in diagnostics], [(2, 18), (3, 25), (5, 26), (6, 31)]
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
