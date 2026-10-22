"""Field modes, metadata, dimensions, and configuration literal contracts."""

import unittest

from tests.support import document
from yambl_compiler.semantics.validation import check


class FieldTests(unittest.TestCase):
    def test_metadata_modes_and_defaults(self):
        source = r"""@title('Security')
@description('Security\'s fields')
entity Security {
    id security_id: string
    @title('Price')
    @description('A # in a string')
    @dimensions(source: code<Sources>, market: string)
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
            ('@title(\'A\') @title(\'B\') attr x: string', 'duplicate metadata: title'),
            (
                '@description(\'A\') @description(\'B\') attr x: string',
                'duplicate metadata: description',
            ),
            (
                '@temporal(as_of_date: date) @temporal(as_of_date: date) scd attr x: decimal',
                'duplicate temporal',
            ),
            ('@temporal(as_of_date: date) config attr x: decimal', 'supported annotation'),
            ('@dimensions(Bad: string) attr x: string', 'dimension names must use snake_case'),
            (
                '@dimensions(source: string, source: string) attr x: string',
                'duplicate dimension: source',
            ),
            (
                '@dimensions(source: string) @dimensions(source: string) attr x: string',
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
        self.assertIn('duplicate metadata', diagnostics[0].message)
