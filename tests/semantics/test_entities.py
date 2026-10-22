"""Extensions merge into entity definitions before inheritance checks."""

import unittest
from pathlib import Path

from tests.support import document
from yambl_compiler.semantics.entities import check_entities
from yambl_compiler.semantics.symbols import resolve_scopes
from yambl_compiler.semantics.validation import check
from yambl_compiler.syntax.parser import children, tokens


class EntityTests(unittest.TestCase):
    def test_final_and_marker_errors(self):
        cases = [
            ('final entity A {} entity B inherits A {}', 'cannot inherit final entity: A'),
            ('final entity A {} extend A {}', 'cannot extend final entity: A'),
            ('entity B inherits ConfigGroup {}', 'cannot inherit final entity: ConfigGroup'),
            ('extend ConfigGroup {}', 'cannot extend final entity: ConfigGroup'),
            ('entity A { @override attr x: string }', 'override requires an inherited field: x'),
            (
                'entity A { @overload attr x: string }',
                'overload requires another branch implementation: x',
            ),
            ('entity A { @override id x: string }', 'field markers require an attr'),
            (
                'entity A { @overload @override attr x: string }',
                'only one overload or override marker is allowed',
            ),
            (
                'entity A { attr x: string } entity B inherits A { @override attr x: integer }',
                'override signature mismatch: x',
            ),
            (
                'entity A {} entity B inherits A { attr x: string } entity C inherits A { @overload attr x: string }',
                'overload requires marked branches with identical signatures: x',
            ),
            (
                'entity A {} entity B inherits A { @overload static attr x: string } entity C inherits A { @overload attr x: string }',
                'overload requires marked branches with identical signatures: x',
            ),
            (
                "entity A {} entity B inherits A { @overload @title('B') attr x: string } entity C inherits A { @overload @title('C') attr x: string }",
                'overload requires marked branches with identical signatures: x',
            ),
            (
                'entity A {} entity B { @overload attr x: string } entity C { @overload attr x: string }',
                'overload requires another branch implementation: x',
            ),
        ]
        for source, message in cases:
            with self.subTest(source=source):
                self.assertIn(message, [d.message for d in check([document(source)])])
        self.assertEqual(check([document('entity A {} final entity B inherits A {}')]), [])

    def test_overload_override_identity_candidates_and_original_scopes(self):
        sources = [
            document(
                'namespace root entity Security { static attr currency: string config attr days: integer = 20 }'
            ),
            document(
                '''namespace bond from root import Security
                entity Bond inherits Security { @overload config attr category: integer = 1 }
                entity DeepBond inherits Bond { @override config attr category: integer = 3 }
                entity DeeperBond inherits DeepBond { @override config attr category: integer = 4 }''',
                'bond.yambl',
            ),
            document(
                '''namespace stock from root import Security
                entity Stock inherits Security {} entity DeepStock inherits Stock {}
                extend DeepStock { @overload config attr category: integer = 2 }
                extend Stock { @override config attr days: integer = 10 }''',
                'stock.yambl',
            ),
            document('entity Other { config attr category: string }', 'other.yambl'),
        ]
        self.assertEqual(check(sources), [])
        index = check_entities(resolve_scopes(sources, []), [])
        category = index.candidates(('root', 'Security'), 'config')['category']
        self.assertEqual(len(category), 4)
        self.assertEqual(
            {s.unit.document.path for s in category}, {Path('bond.yambl'), Path('stock.yambl')}
        )
        identity = index.identities[(('bond', 'Bond'), 'category')]
        for key in [('bond', 'DeepBond'), ('bond', 'DeeperBond'), ('stock', 'DeepStock')]:
            self.assertEqual(index.identities[(key, 'category')], identity)
        self.assertEqual(
            index.identities[(('stock', 'Stock'), 'days')], (('root', 'Security'), 'days')
        )
        self.assertEqual(set(index.candidates(('root', 'Security'), 'static')), {'currency'})
        self.assertEqual(set(index.candidates(('', 'ConfigGroup'), 'static')), {'entity_type'})
        self.assertEqual(index.field_types(('absent', 'Absent')), {})
        self.assertEqual(check(list(reversed(sources))), [])

    def test_builtin_source_and_reserved_names(self):
        from yambl_compiler.semantics.entities import BUILTIN_SOURCE

        source = document(BUILTIN_SOURCE)
        entities = children(source.tree, 'entity')
        self.assertEqual(len(entities), 3)
        self.assertEqual(str(tokens(entities[-1])[0]), 'final')
        for name in ('Entity', 'ConfigGroup'):
            self.assertIn(
                'duplicate or reserved declaration: ' + name,
                [d.message for d in check([document(f'entity {name} {{}}')])],
            )

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
        entities = check_entities(units, diagnostics).fields
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
