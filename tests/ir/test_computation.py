"""Validate portable calculation structure and inferred dependency contracts."""

import json
from dataclasses import replace

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository, validate_ir
from yambl_compiler.ir.codec import dumps, loads
from yambl_compiler.ir.restoring import restore
from yambl_compiler.ir.validation import structural
from yambl_compiler.languages.yambl.computation.records import Argument, Case
from yambl_compiler.languages.yambl.syntax.parser import children


class ComputationIRTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write(
            "a.yambl",
            "entity E { id key: string snapshot attr price: decimal computed attr total: decimal { return self.price::value(as_of_date=input.day) + 1 } }",
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
        self.assertEqual({a.operation for a in self.field.calculation.accesses}, {"value"})
        wire = json.loads(dumps(self.model))
        for definition in wire["definitions"]:
            for field in definition["fields"]:
                if field["calculation"]:
                    field["calculation"]["accesses"][0]["operation"] = "at"
        with self.assertRaisesRegex(ValueError, r"\$.definitions\[0\].fields\[\d+\].calculation"):
            loads(json.dumps(wire))
        wire["format_revision"] = 5
        with self.assertRaisesRegex(ValueError, "unsupported IR revision; recompile"):
            loads(json.dumps(wire))
        self.assertEqual(self.field.calculation.inputs[0].sites[0].path, "a.yambl")
        self.assertNotIn(str(self.root), dumps(self.model))
        self.write("a.yambl", "invalid source after compilation")
        self.assertEqual(validate_ir(loads(dumps(self.model))), ())

    def test_pair_defaults_and_description_round_trip_without_sources(self):
        self.write(
            'a.yambl',
            "entity E { id key: string snapshot attr price: decimal @extra_input_params(day: date = '2026-01-01', 'Valuation day') computed attr total: decimal { return self.price::value(as_of_date=input.day ?? input.other_day) } }",
        )
        model = compile_repository(self.root)
        self.write('a.yambl', 'source intentionally unavailable')
        restored = loads(dumps(model))
        self.assertEqual(validate_ir(restored), ())
        self.assertEqual(dumps(restored), dumps(model))
        field = next(
            f
            for d in restored.definitions
            if d.identity == 'E'
            for f in d.fields
            if f.category == 'computed'
        )
        self.assertEqual([p.name for p in field.calculation.inputs], ['day'])
        self.assertEqual(field.decorators[0].arguments[0].default.literal.text, '2026-01-01')

    def test_dimension_extension_preserves_lexical_scope_and_source(self):
        self.write(
            'a.yambl',
            "namespace core entity E { id key: string dimension direct { value place: string } } extend E { dimension same_file { value x: string } }",
        )
        self.write(
            'extension.yambl',
            "namespace core extend E { dimension prices { value source: code<'PriceSources'> } @dimension(prices) snapshot attr price: decimal }",
        )
        self.write(
            'dimension_only.yambl',
            "namespace core extend E { dimension ratings { value agency: code<'RatingSources'> } }",
        )
        model = compile_repository(self.root)
        entity = next(d for d in model.definitions if d.identity == 'core.E')
        dimensions = {d.name: d for d in entity.dimensions}
        self.assertEqual(dimensions['direct'].source.path, 'a.yambl')
        self.assertEqual(dimensions['prices'].source.path, 'extension.yambl')
        self.assertEqual(dimensions['same_file'].source.path, 'a.yambl')
        self.assertEqual(dimensions['ratings'].source.path, 'dimension_only.yambl')
        self.assertEqual(
            {d.name: d.sites[0].path for d in model.external_dictionaries},
            {'core.PriceSources': 'extension.yambl', 'core.RatingSources': 'dimension_only.yambl'},
        )
        self.write('a.yambl', 'source unavailable')
        self.write('extension.yambl', 'source unavailable')
        self.write('dimension_only.yambl', 'source unavailable')
        self.assertEqual(validate_ir(loads(dumps(model))), ())

        direct = dimensions['direct'].source
        enclosing = replace(
            entity,
            source=replace(entity.source, end_line=direct.end_line, end_column=direct.end_column),
        )
        widened = replace(
            model, definitions=tuple(enclosing if d == entity else d for d in model.definitions)
        )
        analysis = restore(widened)
        self.assertFalse([d for d in analysis.diagnostics if d.severity == 'error'])
        owner = next(
            unit
            for unit in analysis.units
            if unit.document.path.as_posix() == 'a.yambl' and children(unit.document.tree, 'entity')
        )
        declaration = children(owner.document.tree, 'entity')[0]
        self.assertEqual(len(children(declaration, 'dimension')), 1)

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
            (replace(node, value="%"), "binary operator"),
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
