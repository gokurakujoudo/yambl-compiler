"""Calculation grammar and independent expression records."""

import unittest

from tests.support import document
from yambl_compiler.syntax.computation import body, restore_body, restore_hint, type_hint
from yambl_compiler.syntax.parser import Diagnostic, children, parse


class CalculationSyntaxTests(unittest.TestCase):
    def test_complete_local_type_annotations_round_trip(self):
        for annotation in [
            'decimal[18, 4]',
            'decimal[precision: 18, scale: 4]',
            "string['x']",
            "string[regex: 'x']",
            'collection<E>',
            "code<'External'>",
            'mapping<attrs<AttrCats.Static + AttrCats.Config, E> as Attr, Attr::type>',
            'type<self.kind::value>',
        ]:
            with self.subTest(annotation=annotation):
                calculation = self.calculation('let x: ' + annotation + ' = 1 return x')
                hint = calculation.bindings[0].annotation
                restored = restore_hint(hint)
                self.assertEqual(type_hint(restored).name, hint.name)
                self.assertEqual(
                    body(restore_body(calculation)).bindings[0].annotation.name, hint.name
                )

    def calculation(self, text):
        doc = document("entity E { computed attr amount: decimal { " + text + " } }")
        field = children(children(doc.tree, "entity")[0], "field")[0]
        return body(children(children(field, "field_body")[0], "computation")[0])

    def test_precedence_locals_and_position(self):
        result = self.calculation("let x = 2 return self.quantity * x + 3")
        self.assertEqual(result.bindings[0].name, "x")
        self.assertEqual(result.bindings[0].expression.value, "2")
        self.assertEqual(result.result.value, "+")
        self.assertEqual(result.result.operands[0].value, "*")
        self.assertEqual(result.result.operands[0].operands[0].kind, "member")
        self.assertGreater(result.result.span.end, result.result.span.start)

    def test_lambda_binding_and_recovery(self):
        result = self.calculation(
            "return self.holdings::at().map(h -> h.price::best_available(as_of_date=input.price_date ?? input.as_of_date)).recover(case EMPTY -> 0, case NO_ENTRY, NO_DATA -> -1, case PARTIAL as p -> p.value)"
        )
        self.assertEqual(result.result.kind, "recover")
        self.assertEqual(result.result.cases[1].statuses, ("NO_ENTRY", "NO_DATA"))
        self.assertEqual(result.result.cases[2].binding, "p")
        receiver = result.result.operands[0].operands[0]
        self.assertEqual(receiver.arguments[0].expression.kind, "lambda")
        nested = receiver.arguments[0].expression.operands[0]
        self.assertEqual(nested.arguments[0].name, "as_of_date")
        self.assertEqual(nested.arguments[0].expression.kind, "binary")

    def test_condition_and_literals(self):
        result = self.calculation(
            "return if not (1 < 2 and true) or false then 'it\\'s' else 1.25e2"
        )
        self.assertEqual(result.result.kind, "conditional")
        self.assertEqual(result.result.operands[0].value, "or")
        self.assertEqual(result.result.operands[1].value, "it's")
        self.assertEqual(result.result.operands[2].kind, "decimal")

    def test_named_dimension(self):
        doc = document(
            "entity E { dimension prices { value source: code<'Prices'> hierarchy hierarchy: Hierarchy = 'Local' fallback_days = self.days } @dimension(prices) snapshot attr price: decimal }"
        )
        dim = children(children(doc.tree, "entity")[0], "dimension")[0]
        self.assertEqual(len(children(dim, "dimension_value")), 1)
        self.assertEqual(len(children(dim, "dimension_hierarchy")), 1)
        self.assertEqual(len(children(dim, "dimension_fallback")), 1)

    def test_invalid_bodies(self):
        for contents in (
            "",
            "let x = 1",
            "return 1 return 2",
            "return 1 let x = 2",
            "x = 1 return x",
            "return 1 % 2",
            "for x in self.a return x",
        ):
            with self.subTest(contents=contents):
                self.assertIsInstance(
                    parse("entity E { computed attr x: decimal { " + contents + " } }"),
                    Diagnostic,
                )


if __name__ == "__main__":
    unittest.main()
