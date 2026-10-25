"""Pure evaluation, lazy branches and exact constant payloads."""

import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal

from tests.support import document
from yambl_compiler.computation.evaluation import StructuredConstant, evaluate
from yambl_compiler.computation.records import TypeHint
from yambl_compiler.syntax.computation import branches, expression
from yambl_compiler.syntax.parser import children


class EvaluationTests(unittest.TestCase):
    def node(self, source):
        return expression(
            branches(children(document('const A: integer = ' + source).tree, 'constant')[0])[-1]
        )

    def test_operators_and_lazy_branches(self):
        for source, expected in [
            ('1', 1),
            ('1.25', Decimal('1.25')),
            ("'x'", 'x'),
            ('false', False),
            ('-2', -2),
            ('not true', False),
            ('1 + 2 * 3', 7),
            ('7 - 2', 5),
            ('1 / 2', 0.5),
            ('-7 // 3', -3),
            ('1 == 1', True),
            ('1 != 2', True),
            ('1 < 2', True),
            ('1 <= 1', True),
            ('2 > 1', True),
            ('2 >= 2', True),
            ('false and 1 // 0', False),
            ('true or 1 // 0', True),
            ('true and false', False),
            ('false or true', True),
            ('if true then 2 else 1 // 0', 2),
            ('if false then 1 // 0 else 3', 3),
            ('round(1.25, mode=\'half_even\', scale=1)', Decimal('1.2')),
            ('[1, 2, 2]', frozenset((1, 2))),
            ("{'a': 1}", {'a': 1}),
            ('A', 5),
            ('Codes.Usd', 5),
        ]:
            with self.subTest(source=source):
                self.assertEqual(evaluate(self.node(source), lambda name: 5), expected)
        for source in ("{'a': 1, 'a': 2}", 'x -> x', 'self.x::value()'):
            with self.assertRaises(ValueError):
                evaluate(self.node(source), lambda name: 1)
        binary = replace(self.node('1 + 1'), value='%')
        with self.assertRaises(ValueError):
            evaluate(binary, lambda name: 1)

    def test_contextual_payloads(self):
        for source, name, expected in [
            ("'2026-01-01'", 'date', date(2026, 1, 1)),
            ('1.25', 'float', 1.25),
            ('1', 'decimal', Decimal(1)),
        ]:
            node = self.node(source)
            node = replace(node, annotation=TypeHint(name, (), (), (), node.span))
            self.assertEqual(evaluate(node, lambda name: 1), expected)
        node = self.node('{value: 1}')
        node = replace(node, annotation=TypeHint('amount', (), (), (), node.span, 'amount'))
        self.assertEqual(
            evaluate(node, lambda name: 1), StructuredConstant('amount', (('value', 1),))
        )
