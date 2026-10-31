"""Contextual literals and pure constant declaration contracts."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.semantics.validation import analyze


class LiteralTests(unittest.TestCase):
    """Check contextual typing, constant scope and compile-time failures."""

    def errors(self, source: str) -> list[str]:
        """Collect semantic errors for one model.

        :param source: Complete model source.
        :returns: Diagnostic messages.
        """
        return [d.message for d in analyze([document(source)]).diagnostics if d.severity == 'error']

    def test_internal_calculation_names_are_reserved_for_models_and_fields(self) -> None:
        """Reject model and field names while permitting calculation locals.

        :returns: None.
        """
        for name in (
            'record',
            'selection',
            'dimensions',
            'partial',
            'sequence',
            'grouped',
            'issues',
            'non_normal',
        ):
            sources = (
                f'value {name} {{}}',
                f'type {name} = integer',
                f'dimension {name} {{ value source: string }}',
                f'@targets(DecoratorTargets.All) decorator @{name}() ',
                f'entity A {{ id {name}: string }}',
                f'entity A {{ attr {name}: integer }}',
                f'entity A {{ computed attr {name}: integer {{ return 1 }} }}',
                f'entity A {{}} extend A {{ attr {name}: integer }}',
                f'value point {{ attr {name}: integer }}',
                f'entity A {{ dimension {name} {{ value source: string }} }}',
                f'entity A {{ attr x: integer lookup {name} {{ x = self.x::value() }} }}',
                f'entity A {{ constraint {name} {{ assert true }} }}',
            )
            for source in sources:
                with self.subTest(name=name, source=source):
                    self.assertIn(f'reserved calculation name: {name}', self.errors(source))
        self.assertEqual(
            self.errors(
                'value point { attr value: integer attr count: integer } '
                'entity A { computed attr x: integer { '
                'let record: point = {value: 1, count: 2} return record.value + record.count } }'
            ),
            [],
        )
        for source in (
            'namespace sequence @targets(DecoratorTargets.All) decorator @tag(record: integer) '
            '@tag(record=1) entity A {}',
            'entity A { attr x: integer dimension d { value sequence: string } '
            'lookup by_x { issues = self.x::value() } }',
            'entity A { @extra_input_param(partial: integer = 1) '
            'computed attr x: integer { let grouped = input.partial return grouped } }',
        ):
            with self.subTest(source=source):
                self.assertEqual(self.errors(source), [])

    def test_valid(self) -> None:
        """Check declarations, collections, structured values and builtin calls.

        :returns: None.
        """
        source = """const OTHER: string = 'Other'
        const RATE: decimal = truncate(1.239, scale=2)
        const COUNT: integer = NEXT + 1
        const NEXT: integer = 2
        value amount { static attr quantity: decimal static attr label: string }
        entity Thing {
            id id: string
            computed attr amount: amount {
                let quantity: decimal = 1.25
                return {quantity: quantity, label: OTHER}
            }
            computed attr numbers: values<integer> { return [1, 2, 2] }
            computed attr empty: mapping<string, decimal> { return {} }
            computed attr refs: collection<Thing> { return ['a', 'b'] }
            computed attr date_value: date { return '2026-01-01' }
            computed attr division: float { return 1 / 3 }
            computed attr floor_value: integer { return -7 // 3 }
            computed attr formatted: string { return format_date(parse_date('2026-01-01')) }
        }"""
        self.assertEqual(self.errors(source), [])

    def test_invalid(self) -> None:
        """Diagnose ambiguous, incompatible and impure declarations.

        :returns: None.
        """
        cases = [
            'const A: integer = B const B: integer = A',
            'const A: integer = 1 // 0',
            "const A: integer = to_integer('1.5')",
            'const A: integer = self.value',
            'const A: integer = ERROR',
            'const wrong: integer = 1',
            'const A: integer = 1 / 2',
            'entity E { computed attr x: integer { let a = [] return 1 } }',
            "entity E { computed attr x: values<integer> { return ['a'] } }",
            'entity E { computed attr x: integer { let a: integer = 1.2 return a } }',
            'entity E { computed attr x: integer { return to_integer() } }',
            "entity E { computed attr x: decimal { return round(1.25, 1) } }",
            'entity E { computed attr x: integer { return to_integer(value=1, 2) } }',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(self.errors(source))

    def test_contextual_edges(self):
        for source in [
            'value item { static attr a: integer } const ITEMS: values<item> = [{a: 1}]',
            'entity E { computed attr x: values<integer> { return NO_ENTRY.recover(case NO_ENTRY -> []) } }',
            'const A: decimal[4, 2] = 1.25',
            'const A: decimal = if true then 1 else 1.25',
            'entity E { computed attr x: decimal { let a = if true then 1 else 1.25 return a } }',
            "@flags decodes Codes { - 1 One: 'One' } const A: code<Codes> = 'All'",
            'const A: bool = if false then true else not false',
            'const A: date = \'2026-01-01\'',
            'const A: float = 1.25',
            'const A: mapping<string, integer> = {\'a\': 1}',
            'const A: values<integer> = [1, 2]',
            "decodes Codes { - 1 Usd: 'Dollar' } const A: code<Codes> = Codes.Usd",
            'value amount { static attr quantity: integer } const A: amount = {quantity: 1} const B: integer = A.quantity',
            'entity E { id key: string computed attr x: values<decimal> { let a = [1, 1.25] return a } }',
            'entity E { id key: string computed attr x: mapping<string, integer> { let a = {\'x\': 1} return a } }',
            'entity E { id key: string computed attr x: collection<E> { let a = [self] return a } }',
            'entity E { computed attr x: values<integer> { return if true then [] else [1] } }',
        ]:
            with self.subTest(source=source):
                self.assertEqual(self.errors(source), [])
        for source in [
            'const A: decimal[4, 2] = 123.45',
            'const A: float = 1e999',
            'value item { static attr a: integer } const A: item = {a: 1} const B: integer = A.missing',
            'value item { static attr a: integer } const A: item = {a: 1} const B: string = to_string(A)',
            'const A: integer = 1 // 0 const B: integer = A',
            'const A: integer = 1.recover(case EMPTY -> 0)',
            'entity E { computed attr x: integer { return missing(1) } }',
            'entity E { computed attr x: integer { return to_integer(true) } }',
            'entity E { computed attr x: integer { return to_integer(extra=1) } }',
            'entity E { computed attr x: integer { return [1] } }',
            'entity E { computed attr x: integer { return {} } }',
            'entity E { computed attr x: integer { let a = {} return 1 } }',
            'entity E { computed attr x: integer { let a = [[1]] return 1 } }',
            'entity E { computed attr x: integer { let a = {1: 2} return 1 } }',
            'entity E { computed attr x: integer { let a = {\'a\': [1]} return 1 } }',
            'entity E { computed attr x: mapping<string, integer> { return {\'a\': 1, \'a\': 2} } }',
            'entity E { computed attr x: mapping<string, integer> { return {\'a\': true} } }',
            'value amount { static attr quantity: integer } entity E { computed attr x: amount { return {quantity: \'bad\'} } }',
            'value amount { static attr quantity: integer } entity E { computed attr x: amount { return {bad: 1} } }',
            'entity E { computed attr x: date { return \'invalid\' } }',
            'entity E { computed attr x: E { return \'key\' } }',
            'entity E { id key: integer computed attr x: E { return \'key\' } }',
            "decodes Codes { - 1 Usd: 'Dollar' } entity E { computed attr x: code<Codes> { return 'NoSuchCode' } }",
            'entity E { computed attr x: bool { return [1] < [2] } }',
            'entity E { computed attr x: decimal { return round(true, 2, \'floor\') } }',
        ]:
            with self.subTest(source=source):
                self.assertTrue(self.errors(source))
