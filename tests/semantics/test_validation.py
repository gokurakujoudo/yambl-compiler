"""Name resolution and built-in type constraints."""

import unittest
from pathlib import Path

from tests.support import document
from yambl_compiler.semantics.validation import check


class ValidationTests(unittest.TestCase):
    def messages(self, *sources):
        return check([document(source, f'{i}.yambl') for i, source in enumerate(sources)])

    def test_current_types_and_namespaces(self):
        sources = [
            'namespace core entity Fund { id fund_id: string attr holding: collection<HoldingEntry> }',
            'namespace core entity HoldingEntry { attr fund: Fund attr security: Security }',
            'namespace core from dictionaries.money import Currencies entity Security { attr currency: code<Currencies> attr dictionary: Decodes attr entries: collection<DecodesEntry> }',
            "namespace dictionaries.money decodes Currencies { - 100 CNY: '人民币' - 200 USD: 'USD' }",
            "entity Types { attr a: bool attr b: integer attr c: float attr d: decimal attr e: date attr f: datetime attr g: string['x#'] attr h: string[regex: 'x'] attr i: decimal[18, 4] attr j: decimal[scale: 4, precision: 18] attr k: values<string> attr m: map<Decodes, decimal> attr n: map<string, Decodes> @temporal(as_of_date: date) snapshot attr total: decimal }",
        ]
        self.assertEqual(self.messages(*sources), [])
        self.assertEqual(self.messages('entity Empty {}'), [])

    def test_import_scope_and_directory_independence(self):
        dictionaries = "namespace enums decodes Currencies { - 1 CNY: 'CNY' }"
        sources = [
            document(dictionaries, 'arbitrary/place.yambl'),
            document(
                'namespace core from enums import Currencies entity A { attr c: code<Currencies> }',
                'unrelated/one.yambl',
            ),
            document(
                'namespace core entity B { attr a: A attr c: code<Currencies> }', 'other/two.yambl'
            ),
        ]
        diagnostics = check(sources)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].path, Path('other/two.yambl'))
        self.assertIn('undefined Decodes: Currencies', diagnostics[0].message)

    def test_invalid_types(self):
        cases = {
            'collection<string>': 'entity reference',
            'values<Thing>': 'value type',
            'values<values<string>>': 'value type',
            'map<integer, string>': 'map keys',
            'map<string, values<string>>': 'cannot be collections',
            'map<code<Currencies>, string>': 'map keys',
            'collection': 'invalid type arguments',
            'collection[2]': 'invalid type arguments',
            'collection<Thing, Thing>': 'invalid type arguments',
            'map<string>': 'invalid type arguments',
            'code<Thing>': 'Decodes instance identifier',
            'code<Currencies<string>>': 'Decodes instance identifier',
            'string<Thing>': 'does not accept generic',
            'decimal[18, scale: 4]': 'invalid parameters',
            'decimal[precision: 18, 4]': 'invalid parameters',
            'decimal[18]': 'invalid parameters',
            "decimal[precision: 'x', scale: 4]": 'invalid parameters',
            'decimal[precision: 18, precision: 4]': 'invalid parameters',
            'string[4]': 'invalid parameters',
            "string[wrong: 'x']": 'invalid parameters',
            'bool[1]': 'invalid parameters',
            'Currencies': 'is an instance',
            'int': 'unknown type',
            'Missing': 'unknown type',
            'collection<Missing>': 'unknown type',
            'values<missing>': 'unknown type',
            'map<Missing, Missing>': 'unknown type',
        }
        for type_source, expected in cases.items():
            with self.subTest(type_source=type_source):
                diagnostics = self.messages(
                    "decodes Currencies { - 1 CNY: 'CNY' } entity Thing {} entity Test { attr x: "
                    + type_source
                    + ' }'
                )
                self.assertTrue(any(expected in item.message for item in diagnostics), diagnostics)
                self.assertTrue(diagnostics)

    def test_declarations_and_annotations(self):
        cases = [
            ('entity bad_name {}', 'PascalCase'),
            ('entity Decodes {}', 'reserved'),
            ('entity A {} entity A {}', 'duplicate'),
            ('from missing import A entity B {}', 'unknown import'),
            ('namespace a from a import A entity A {}', 'conflicts'),
            ('entity A { attr X: string }', 'snake_case'),
            ('entity A { id x: string attr x: decimal }', 'duplicate field'),
            ('entity A { @temporal(as_of_date: date) attr x: decimal }', 'supported annotation'),
            ('entity A { @temporal(day: date) snapshot attr x: decimal }', 'supported annotation'),
            ("decodes Currencies { - 1 CNY: 'A' - 2 CNY: 'B' }", 'duplicate code'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertTrue(any(expected in item.message for item in self.messages(source)))
        self.assertEqual(
            self.messages(
                'entity A { id x: snowflake_id attr ids: values<snowflake_id> attr ids_by_name: map<string, snowflake_id> }'
            ),
            [],
        )
        self.assertIn(
            'entity reference',
            self.messages('entity A { attr ids: collection<snowflake_id> }')[0].message,
        )
        self.assertIn(
            'map keys', self.messages('entity A { attr ids: map<snowflake_id, string> }')[0].message
        )
