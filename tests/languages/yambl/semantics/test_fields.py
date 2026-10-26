"""Field modes, metadata, dimensions, and configuration literal contracts."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.semantics.validation import check


class FieldTests(unittest.TestCase):
    def test_dimension_selectors_and_parameter_namespace(self):
        self.assertEqual(
            check(
                [
                    document(
                        "entity A { dimension dimension_39 { value source: string['x'] hierarchy preference: Hierarchy } dimension dimension_94 { value kind: string }  @temporal(as_of_date: date) @dimension(dimension_39) @dimension(dimension_94) snapshot attr x: decimal }"
                    )
                ]
            ),
            [],
        )
        for field, message in [
            (
                "dimension dimension_11 { value source: integer }  @dimension(dimension_11) attr x: string",
                "dimension value must be string or code",
            ),
            (
                "dimension dimension_11 { value source: string hierarchy selector: string }  @dimension(dimension_11) attr x: string",
                "dimension hierarchy requires Hierarchy",
            ),
            (
                "dimension dimension_11 { value source: string hierarchy selector: Hierarchy<string> }  @dimension(dimension_11) attr x: string",
                'Hierarchy does not accept generic arguments',
            ),
            (
                '@dimension(source: string, selector: Hierarchy, extra: string) attr x: string',
                "inline @dimension is no longer supported; declare dimension name { ... } and use @dimension(name)",
            ),
            (
                "dimension dimension_11 { value source: string }  @dimension(dimension_11) config attr x: string",
                'decorator dimension does not allow this target',
            ),
            (
                "dimension dimension_11 { value source: string }  @dimension(dimension_11) config attr x: string",
                'decorator dimension does not allow this target',
            ),
            (
                "dimension dimension_11 { value source: string }  @dimension(dimension_11) id x: string",
                'decorator dimension does not allow this target',
            ),
            (
                "dimension dimension_39 { value as_of_date: string }  @temporal(as_of_date: date) @dimension(dimension_39) snapshot attr x: string",
                'duplicate dimension: as_of_date',
            ),
            (
                "dimension dimension_11 { value source: string hierarchy h: Hierarchy } dimension dimension_52 { value other: string hierarchy h: Hierarchy }  @dimension(dimension_11) @dimension(dimension_52) attr x: string",
                'duplicate dimension: h',
            ),
        ]:
            with self.subTest(field=field):
                self.assertIn(
                    message,
                    [d.message for d in check([document('entity A { ' + field + ' }')])],
                )

    def test_metadata_modes_and_defaults(self):
        source = "@title('Security')\n@description('Security\\'s fields')\nentity Security { dimension dimension_159 { value source: code<Sources> } dimension dimension_193 { value market: string } \n    id security_id: string\n    @title('Price')\n    @description('A # in a string')\n    @dimension(dimension_159) @dimension(dimension_193)\n    @temporal(as_of_date: date)\n    snapshot attr price: decimal\n    @temporal(as_of_date: date)\n    scd attr notional_value: decimal\n    config attr days: integer = 20\n    config attr negative: integer = -1\n    config attr fraction: decimal[18, 4] = -1.25\n    config attr estimate: float = 1.2e-3\n    config attr large: float = 2e3\n    config attr whole: decimal = 2\n    config attr active: bool = true\n    config attr inactive: bool = false\n    config attr label: string = 'Bob\\'s # label'\n    config attr seed: snowflake_id = 123\n    config attr no_default: integer\n}\n@title('Sources')\ndecodes Sources { - 1 PRIMARY: 'Main' }\n"
        self.assertEqual(check([document(source)]), [])

    def test_invalid_field_annotations_and_defaults(self):
        cases = [
            ('@title(\'A\') @title(\'B\') attr x: string', 'duplicate decorator: title'),
            (
                '@description(\'A\') @description(\'B\') attr x: string',
                'duplicate decorator: description',
            ),
            (
                '@temporal(as_of_date: date) @temporal(as_of_date: date) scd attr x: decimal',
                'duplicate decorator: temporal',
            ),
            (
                '@temporal(as_of_date: date) config attr x: decimal',
                'decorator temporal does not allow this target',
            ),
            (
                "dimension dimension_11 { value Bad: string }  @dimension(dimension_11) attr x: string",
                "dimension names must use snake_case",
            ),
            (
                "dimension dimension_11 { value source: string hierarchy source: string }  @dimension(dimension_11) attr x: string",
                'duplicate dimension: source',
            ),
            (
                "dimension dimension_11 { value source: string } dimension dimension_38 { value source: string }  @dimension(dimension_11) @dimension(dimension_38) attr x: string",
                'duplicate dimension: source',
            ),
            ('attr x: integer = 1', 'only on config attr'),
            ('config attr x: integer = \'one\'', 'incompatible config default'),
            ('config attr x: date = \'2026-01-01\'', 'incompatible config default'),
            ('config attr x: integer = 1.5', 'incompatible config default'),
            ('config attr x: bool = 1', 'incompatible config default'),
            ('config attr x: Security = 1', 'incompatible config default'),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                diagnostics = check([document('entity Security { ' + body + ' }')])
                self.assertTrue(any(message in item.message for item in diagnostics), diagnostics)
        diagnostics = check([document('entity A {} extend A { id x: string }')])
        self.assertIn('extend may add attributes', diagnostics[0].message)
        diagnostics = check([document("@title('A') @title('B') entity A {}")])
        self.assertIn('duplicate decorator', diagnostics[0].message)
