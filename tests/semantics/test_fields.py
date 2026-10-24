"""Field modes, metadata, dimensions, and configuration literal contracts."""

import unittest

from tests.support import document
from yambl_compiler.semantics.validation import check


class FieldTests(unittest.TestCase):
    def test_dimension_selectors_and_parameter_namespace(self):
        self.assertEqual(
            check(
                [
                    document(
                        "entity A { @temporal(as_of_date: date) @dimension(source: string['x'], preference: Hierarchy) @dimension(kind: string) snapshot attr x: decimal }"
                    )
                ]
            ),
            [],
        )
        for field, message in [
            (
                '@dimension(source: integer) attr x: string',
                'incompatible decorator pair type',
            ),
            (
                '@dimension(source: string, selector: string) attr x: string',
                'incompatible decorator pair type',
            ),
            (
                '@dimension(source: string, selector: Hierarchy<string>) attr x: string',
                'Hierarchy does not accept generic arguments',
            ),
            (
                '@dimension(source: string, selector: Hierarchy, extra: string) attr x: string',
                'unexpected argument for decorator: dimension',
            ),
            (
                '@dimension(source: string) config attr x: string',
                'decorator dimension does not allow this target',
            ),
            (
                '@dimension(source: string) config attr x: string',
                'decorator dimension does not allow this target',
            ),
            (
                '@dimension(source: string) id x: string',
                'decorator dimension does not allow this target',
            ),
            (
                '@temporal(as_of_date: date) @dimension(as_of_date: string) snapshot attr x: string',
                'duplicate dimension: as_of_date',
            ),
            (
                '@dimension(source: string, h: Hierarchy) @dimension(other: string, h: Hierarchy) attr x: string',
                'duplicate dimension: h',
            ),
        ]:
            with self.subTest(field=field):
                self.assertIn(
                    message, [d.message for d in check([document('entity A { ' + field + ' }')])]
                )

    def test_metadata_modes_and_defaults(self):
        source = r"""@title('Security')
@description('Security\'s fields')
entity Security {
    id security_id: string
    @title('Price')
    @description('A # in a string')
    @dimension(source: code<Sources>) @dimension(market: string)
    @temporal(as_of_date: date)
    snapshot attr price: decimal
    @temporal(as_of_date: date)
    scd attr notional_value: decimal
    config attr days: integer = 20
    config attr negative: integer = -1
    config attr fraction: decimal[18, 4] = -1.25
    config attr estimate: float = 1.2e-3
    config attr large: float = 2e3
    config attr whole: decimal = 2
    config attr active: bool = true
    config attr inactive: bool = false
    config attr label: string = 'Bob\'s # label'
    config attr seed: snowflake_id = 123
    config attr no_default: integer
}
@title('Sources')
decodes Sources { - 1 PRIMARY: 'Main' }
"""
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
            ('@dimension(Bad: string) attr x: string', 'pair keys must use snake_case'),
            (
                '@dimension(source: string, source: string) attr x: string',
                'duplicate dimension: source',
            ),
            (
                '@dimension(source: string) @dimension(source: string) attr x: string',
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
