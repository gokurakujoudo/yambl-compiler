"""Extensions merge into entity definitions before inheritance checks."""

import unittest
from pathlib import Path

from tests.support import document
from yambl_compiler.semantics.entities import check_entities
from yambl_compiler.semantics.symbols import resolve_scopes
from yambl_compiler.semantics.validation import check
from yambl_compiler.syntax.parser import children, tokens


class EntityTests(unittest.TestCase):
    def test_extension_merge_retains_attribute_metadata_and_source(self):
        sources = [
            document(
                "namespace extras from core import Security\nextend Security {\n    @title('Price')\n    attr price: decimal\n}",
                'extras.yambl',
            ),
            document(
                'namespace core entity Bond inherits Security { attr coupon: decimal }',
                'bond.yambl',
            ),
            document('namespace core entity Security { id security_id: string }', 'base.yambl'),
        ]
        diagnostics = []
        units = resolve_scopes(sources, diagnostics)
        entities = check_entities(units, diagnostics)
        self.assertEqual(diagnostics, [])
        self.assertEqual(set(entities), {('core', 'Security'), ('core', 'Bond')})
        self.assertEqual(set(entities[('core', 'Security')]), {'security_id', 'price'})
        price = entities[('core', 'Security')]['price']
        self.assertEqual(price.unit.document.path, Path('extras.yambl'))
        body = children(price.declaration, 'field_body')[0]
        self.assertEqual((tokens(body)[0].line, tokens(body)[0].column), (4, 5))
        self.assertEqual(str(tokens(children(price.declaration, 'metadata')[0])[-2]), "'Price'")
        self.assertEqual(check(sources), [])
        self.assertEqual(check(list(reversed(sources))), [])

    def test_invalid_targets_cycles_and_duplicate_fields(self):
        cases = [
            ('entity A inherits Missing {}', 'inherits requires a visible entity'),
            ('decodes Names {} entity A inherits Names {}', 'inherits requires a visible entity'),
            ('entity A inherits Decodes {}', 'Decodes is sealed'),
            ('entity A inherits DecodesEntry {}', 'built-in inheritance is unsupported'),
            ('extend Missing { attr x: string }', 'extend requires a visible'),
            ('decodes Names {} extend Names { attr x: string }', 'extend requires a visible'),
            ('extend Decodes { attr x: string }', 'extend requires a visible'),
            ('entity A inherits A {}', 'inheritance cycle'),
            (
                'entity A inherits B {} entity B inherits C {} entity C inherits A {}',
                'inheritance cycle',
            ),
            ('entity A { attr x: string } extend A { attr x: string }', 'duplicate field: x'),
            (
                'entity A {} extend A { attr x: string } extend A { attr x: string }',
                'duplicate field: x',
            ),
            (
                'entity A { attr x: string } entity B inherits A { attr x: string }',
                'duplicate inherited field: x',
            ),
            (
                'entity A {} entity B inherits A {} entity C inherits B {} extend A { attr x: string } extend C { attr x: string }',
                'duplicate inherited field: x',
            ),
        ]
        for source, message in cases:
            with self.subTest(source=source):
                diagnostics = check([document(source)])
                self.assertTrue(any(message in item.message for item in diagnostics), diagnostics)
                self.assertTrue(all(item.severity == 'error' for item in diagnostics))

    def test_extension_does_not_export_imports_or_create_names(self):
        sources = [
            document('namespace core entity Security {}'),
            document(
                'namespace extras from core import Security extend Security { attr a: string }'
            ),
            document('namespace extras extend Security { attr b: string }', 'unimported.yambl'),
        ]
        diagnostics = check(sources)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].path, Path('unimported.yambl'))
        self.assertIn('extend requires a visible', diagnostics[0].message)
