"""Validate portable calculation structure and inferred dependency contracts."""

from dataclasses import replace

from tests.support import FilesystemCase
from yambl_compiler.computation.records import Argument, Case
from yambl_compiler.ir import compile_repository, validate_ir
from yambl_compiler.ir.codec import dumps, loads
from yambl_compiler.ir.validation import structural


class ComputationIRTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write(
            "a.yambl",
            "entity E { id key: string snapshot attr price: decimal computed attr total: decimal { return self.price::at(as_of_date=input.day) + 1 } }",
        )
        self.model = compile_repository(self.root)
        self.entity = next(d for d in self.model.definitions if d.identity == "E")
        self.field = next(f for f in self.entity.fields if f.category == "computed")

    def changed(self, expression):
        calculation = replace(
            self.field.calculation,
            body=replace(self.field.calculation.body, result=expression),
        )
        field = replace(self.field, calculation=calculation)
        entity = replace(
            self.entity,
            fields=tuple(field if f == self.field else f for f in self.entity.fields),
        )
        return replace(
            self.model,
            definitions=tuple(entity if d == self.entity else d for d in self.model.definitions),
        )

    def test_portable_sources_and_independent_validation(self):
        self.assertEqual(validate_ir(loads(dumps(self.model))), ())
        self.assertEqual(self.field.calculation.inputs[0].sites[0].path, "a.yambl")
        self.assertNotIn(str(self.root), dumps(self.model))
        self.write("a.yambl", "invalid source after compilation")
        self.assertEqual(validate_ir(loads(dumps(self.model))), ())

    def test_malformed_calculation_nodes_report_json_paths(self):
        node = self.field.calculation.body.result
        literal = node.operands[1]
        cases = [
            (
                replace(node, span=replace(node.span, start=-1)),
                "invalid calculation span",
            ),
            (
                replace(node, span=replace(node.span, end=node.span.start - 1)),
                "invalid calculation span",
            ),
            (
                replace(node, span=replace(node.span, line=0)),
                "invalid calculation span",
            ),
            (
                replace(node, span=replace(node.span, end_line=1, end_column=1)),
                "invalid calculation span",
            ),
            (replace(node, operands=()), "operand count"),
            (
                replace(node, arguments=(Argument("", literal, literal.span),)),
                "only calls",
            ),
            (
                replace(node, cases=(Case(("EMPTY",), "", literal, literal.span),)),
                "only recover",
            ),
            (
                replace(node, kind="unary", value="/", operands=(literal,)),
                "unary operator",
            ),
            (replace(node, value="/"), "binary operator"),
            (replace(literal, kind="decimal", value="NaN"), "calculation number"),
            (replace(literal, kind="decimal", value="broken"), "calculation number"),
            (replace(literal, kind="integer", value="1.2"), "calculation number"),
            (replace(literal, kind="bool", value="maybe"), "calculation boolean"),
        ]
        for bad, expected in cases:
            with self.subTest(expected=expected):
                problems = structural(self.changed(bad))
                self.assertTrue(
                    any(
                        expected in issue and ".calculation.body.result" in issue
                        for issue in problems
                    ),
                    problems,
                )
        definitions = tuple(
            replace(
                d,
                fields=tuple(
                    replace(f, category="static") if f == self.field else f for f in d.fields
                ),
            )
            for d in self.model.definitions
        )
        self.assertTrue(
            any(
                "only computed" in issue
                for issue in structural(replace(self.model, definitions=definitions))
            )
        )
