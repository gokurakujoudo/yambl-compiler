"""Only type and dictionary declarations introduce namespace symbols."""

import unittest

from tests.support import document
from yambl_compiler.semantics.symbols import resolve_scopes


class SymbolTests(unittest.TestCase):
    def test_extensions_do_not_introduce_namespace_symbols(self):
        diagnostics = []
        units = resolve_scopes(
            [
                document(
                    'namespace extras from core import Security extend Security { attr x: string }'
                ),
                document('namespace core entity Security {}'),
                document('namespace extras'),
            ],
            diagnostics,
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(units[0].scope['Security'].key, ('core', 'Security'))
        self.assertNotIn('Security', units[2].scope)
        self.assertIn('field_mapping', units[2].scope)
