"""Behavioral contracts for calculation inference and dependency checking."""

import unittest
from dataclasses import replace

from tests.support import document
from yambl_compiler.languages.yambl.computation.evaluation import StructuredConstant
from yambl_compiler.languages.yambl.semantics.computation import Checker, Computations, Input
from yambl_compiler.languages.yambl.semantics.types import Resolved
from yambl_compiler.languages.yambl.semantics.validation import analyze


class ComputationTests(unittest.TestCase):
    def test_finite_value_requires_a_declared_scalar_or_structure(self):
        analysis = self.analyze('value point { attr x: integer } entity A {}')
        unit = analysis.units[0]
        computations = unit.world.computations
        point = StructuredConstant('point', (('x', 2),))
        result = computations.finite_value(unit, Resolved('point', key=('', 'point')), point)
        self.assertEqual(result.target, ('', 'point'))
        self.assertEqual(result.value[0][1].value, 2)
        with self.assertRaisesRegex(ValueError, 'structured finite value requires a declared type'):
            computations.finite_value(unit, Resolved('point'), point)
        with self.assertRaisesRegex(ValueError, 'finite values must be non-collection values'):
            computations.finite_value(unit, Resolved('integer'), [1, 2])

    def test_best_available_requires_explicit_value_and_checks_dimensions(self):
        prefix = '''decodes Sources { - 1 Feed: 'Feed' }
        entity E { dimension feed { value source: code<Sources> hierarchy order: Hierarchy }
            @dimension(feed) attr price: integer
        '''
        valid = prefix + '''computed attr source: code<Sources> {
            let selected = self.price::best_available()
            return selected.dimensions.source
        } }'''
        self.assertEqual(self.errors(valid), [])
        invalid = (
            prefix + 'computed attr amount: integer { return self.price::best_available() * 2 } }'
        )
        self.assertTrue(self.errors(invalid))
        missing = (
            prefix
            + 'computed attr source: string { return self.price::best_available().dimensions.missing } }'
        )
        self.assertTrue(
            any('unknown dimensions member: missing' in e for e in self.errors(missing))
        )

    def test_abstract_computed_contract_is_inferred_from_implementations(self):
        source = """
        abstract entity Root {
            snapshot attr amount: integer
            abstract computed attr value: integer
            computed attr total: integer { return self.value::value() }
        }
        entity First inherits Root {
            @override computed attr value: integer { return self.amount::value() }
        }
        entity Second inherits Root {
            @override computed attr value: integer { return self.amount::value() }
        }
        """
        result = self.analyze(source)
        self.assertEqual([d for d in result.diagnostics if d.severity == 'error'], [])
        self.assertEqual(
            [p.name for p in result.calculations[(("", "Root"), "total")].inputs],
            ['as_of_date'],
        )
        mismatch = source.replace(
            'return self.amount::value() }\n        }\n        entity Second',
            'return 1 }\n        }\n        entity Second',
        )
        self.assertTrue(any('signature mismatch' in message for message in self.errors(mismatch)))

    def test_abstract_contract_order_defaults_and_cycles(self):
        root = '''abstract entity Root {
            @temporal(day: date) snapshot attr amount: integer
            abstract computed attr value: integer
            computed attr total: integer { return self.value::value() }
        }'''
        first = '''entity First inherits Root {
            @override computed attr value: integer { return self.amount::value(day=input.day) }
        }'''
        second = first.replace('First', 'Second')
        for source in (root + first + second, root + second + first, first + second + root):
            with self.subTest(source=source):
                result = self.analyze(source)
                self.assertEqual(
                    [d.message for d in result.diagnostics if d.severity == 'error'], []
                )
                self.assertEqual(
                    [p.name for p in result.calculations[(('', 'Root'), 'total')].inputs], ['day']
                )
        default_first = first.replace('day=input.day', 'day=input.day ?? input.fallback')
        default_second = second.replace('day=input.day', 'day=input.day ?? input.other')
        for source in (
            root + default_first + default_second,
            root + default_second + default_first,
        ):
            with self.subTest(source=source):
                result = self.analyze(source)
                conflicts = [
                    d for d in result.diagnostics if 'parameter signature mismatch' in d.message
                ]
                self.assertTrue(conflicts)
                self.assertTrue(
                    all('conflicts with' in d.message and d.line > 0 for d in conflicts)
                )
        cyclic = first.replace('self.amount::value(day=input.day)', 'self.total::value()')
        self.assertTrue(any('dependency cycle' in e for e in self.errors(root + cyclic + second)))

    def test_abstract_contract_accepts_extra_input_from_implementations(self):
        source = '''abstract entity Root { abstract computed attr value: integer }
        entity First inherits Root {
            @override @extra_input_param(n: integer = 2)
            computed attr value: integer { return input.n }
        }
        entity Second inherits Root {
            @override @extra_input_param(n: integer = 2)
            computed attr value: integer { return input.n }
        }'''
        self.assertEqual(self.errors(source), [])
        changed = source.replace('n: integer = 2', 'n: integer = 3', 1)
        self.assertTrue(any('parameter signature mismatch' in e for e in self.errors(changed)))

    def test_explicit_retrieval_and_removed_at(self):
        prefix = "entity E { id key: string static attr n: integer static attr child: E static attr items: collection<E> "
        for body in (
            "let x = self.n return x",
            "return self.n",
            "return self.n + 1",
            "return self.n.recover(case EMPTY -> 0)",
            "return self.items::value().sum_by(h -> h.n)",
            "let x = self.child return x.n::value()",
            "return to_integer(self.n)",
        ):
            with self.subTest(body=body):
                self.assertIn(
                    "field retrieval requires ::value(...) or ::best_available(...)",
                    self.errors(prefix + "computed attr result: integer { " + body + " } }"),
                )
        self.assertIn(
            "field retrieval requires ::value(...) or ::best_available(...)",
            self.errors(
                "entity E { snapshot attr n: integer computed attr result: integer { return self.n } }"
            ),
        )
        navigation = self.analyze(
            "entity E { snapshot attr child: E static attr n: integer computed attr result: integer { return self.child.n::value() } }"
        )
        self.assertEqual([d for d in navigation.diagnostics if d.severity == "error"], [])
        self.assertEqual(
            [p.name for p in navigation.calculations[(("", "E"), "result")].inputs], ["as_of_date"]
        )
        for category in ("static", "computed"):
            declaration = (
                "static attr n: integer"
                if category == "static"
                else "computed attr n: integer { return 1 }"
            )
            self.assertEqual(
                self.errors(
                    "entity E { "
                    + declaration
                    + " computed attr result: integer { return self.n::at() } }"
                ),
                ["::at(...) was removed; use ::value(...) instead"],
            )
        self.assertEqual(
            self.errors(
                prefix
                + "computed attr result: integer { let x = self.child::value() return self.child.n::value() + x.n::value() } }"
            ),
            [],
        )
        self.assertEqual(
            self.errors(
                "value pair { static attr n: integer } entity E { computed attr result: integer { let x: pair = {n: 1} return x.n } }"
            ),
            [],
        )

    def test_subtype_constant_requires_entity_and_literal_accepts_known_member(self):
        errors = self.errors('const BAD: string = Missing::subtypes.Child')
        self.assertIn('not a pure constant dependency: Missing', errors)
        self.assertIn(
            'constant expressions cannot read fields, inputs or non-normal results', errors
        )
        valid = self.analyze(
            "entity Root {} entity Child inherits Root {} entity Test { computed attr result: code<Root::subtypes> { return 'Child' } }"
        )
        self.assertEqual([d for d in valid.diagnostics if d.severity == 'error'], [])
        self.assertIn((('', 'Test'), 'result'), valid.calculations)

    def test_division_result_types(self):
        self.assertEqual(
            self.errors(
                'entity E { computed attr x: float { return 1 / 3 } computed attr y: integer { return -7 // 3 } }'
            ),
            [],
        )
        self.assertTrue(self.errors('entity E { computed attr x: decimal { return 1 / 3 } }'))
        self.assertTrue(self.errors("entity E { computed attr x: float { return 'one' / 3 } }"))

    def test_declared_inputs_defaults_and_descriptions(self):
        self.assertTrue(
            any(
                "?? requires" in d
                for d in self.errors("entity E { computed attr x: integer { return 1 ?? 2 } }")
            )
        )
        source = """entity E {
            snapshot attr value: integer
            @extra_input_param(day: date = '2026-01-01', 'A date')
            @extra_input_param(count: integer = 2, description='An integer')
            @extra_input_param(rate: decimal = 1.2)
            @extra_input_param(enabled: bool = true)
            @extra_input_param(label: string = 'x')
            computed attr result: integer {
                return self.value::value(as_of_date=input.day ?? input.unused_date) + input.count
            }
        }"""
        result = self.analyze(source)
        self.assertEqual([d.message for d in result.diagnostics if d.severity == 'error'], [])
        inputs = {p.name: p for p in result.calculations[(('', 'E'), 'result')].inputs}
        self.assertNotIn('unused_date', inputs)
        self.assertEqual(inputs['day'].default.value, '2026-01-01')
        self.assertEqual(inputs['rate'].default.kind, 'decimal')
        self.assertFalse(inputs['count'].required)
        for metadata, message in [
            (
                '@extra_input_param(day: date) @extra_input_param(day: date)',
                'duplicate extra input',
            ),
            ("@extra_input_param(day: date = '2026-02-30')", 'valid YYYY-MM-DD'),
            ("@extra_input_param(day: date = '20260101')", 'valid YYYY-MM-DD'),
            ('@extra_input_param(day: date = EMPTY)', 'incompatible decorator'),
            ('@extra_input_param(day: collection<E>)', 'incompatible decorator pair'),
            ('@extra_input_param(day: dimension_ref)', 'incompatible decorator pair'),
            ('@extra_input_param()', 'missing decorator argument'),
            ('@extra_input_param(best_effort: bool)', 'reserved calculation parameter'),
        ]:
            with self.subTest(metadata=metadata):
                self.assertTrue(
                    any(
                        message in d
                        for d in self.errors(
                            f'entity E {{ {metadata} computed attr x: integer {{ return 0 }} }}'
                        )
                    )
                )

    def test_default_local_expansion_and_shared_dependency_graph(self):
        result = self.analyze(
            'entity E { static attr day: date snapshot attr x: integer computed attr y: integer { let d = self.day::value() return self.x::value(as_of_date=input.day ?? d) } }'
        )
        self.assertEqual([d.message for d in result.diagnostics if d.severity == "error"], [])
        calculation = result.calculations[(("", "E"), "y")]
        self.assertEqual(calculation.inputs[0].default.operands[0].operands[0].value, "day")
        checker = Checker(Computations(result.entities, []), ("", "E"), result.units[-1])
        node = calculation.body.result
        checker.issue(node, "duplicate")
        checker.issue(node, "duplicate")
        self.assertEqual(len(checker.computations.diagnostics), 1)
        literal = replace(node, kind="integer", value="1", operands=(), arguments=(), cases=())
        self.assertEqual(
            checker.invoke(replace(node, operands=(literal,)), {}).type.kind, "unknown"
        )
        root = replace(literal, kind="name", value="input")

        def ref(name):
            return replace(literal, kind="member", value=name, operands=(root,))

        def pair(left, right):
            return replace(literal, kind="binary", value="+", operands=(ref(left), ref(right)))

        checker.inputs = {
            "a": Input("a", Resolved("integer"), True, pair("b", "c")),
            "b": Input("b", Resolved("integer"), True, ref("d")),
            "c": Input("c", Resolved("integer"), True, ref("d")),
            "d": Input("d", Resolved("integer"), True, None),
        }
        checker.computations.diagnostics.clear()
        checker.check_defaults()
        self.assertEqual(checker.computations.diagnostics, [])

    def test_named_dimension_selection_conflicts(self):
        source = "entity E { dimension a { value av: string hierarchy ah: Hierarchy } dimension b { value bv: string hierarchy bh: Hierarchy } @dimension(a) @dimension(b) static attr x: integer computed attr y: integer { return self.x::best_available().value } }"
        self.assertTrue(any("one automatic Hierarchy" in e for e in self.errors(source)))
        self.assertEqual(
            self.errors(source.replace("best_available()", "best_available(av='fixed')")),
            [],
        )
        invalid = "entity E { @dimension(missing) snapshot attr x: integer computed attr y: integer { return self.x::best_available().value } }"
        self.assertTrue(any("unknown dimension" in e for e in self.errors(invalid)))

    def analyze(self, source):
        return analyze([document(source)])

    def errors(self, source):
        return [d.message for d in self.analyze(source).diagnostics if d.severity == "error"]

    def test_inference_binding_and_reuse(self):
        analysis = self.analyze("""
            entity Security {
                dimension prices { value source: code<'Sources'> hierarchy hierarchy: Hierarchy = 'Local' fallback_days = self.days::value() }
                config attr days: integer = 20
                @temporal(as_of_date: date) @dimension(prices)
                snapshot attr price: decimal
            }
            entity Holding {
                static attr security: Security
                static attr quantity: decimal
                computed attr amount: decimal { return self.quantity::value() * self.security.price::best_available().value }
            }
            entity Fund {
                @temporal(as_of_date: date) snapshot attr holdings: collection<Holding>
                computed attr amount: decimal {
                    let holdings = self.holdings::value()
                    return holdings.sum_by(h -> h.amount::value(as_of_date=input.price_date ?? input.as_of_date))
                }
            }
        """)
        self.assertEqual([d.message for d in analysis.diagnostics if d.severity == "error"], [])
        parameters = {p.name: p for p in analysis.calculations[(("", "Fund"), "amount")].inputs}
        self.assertEqual(set(parameters), {"as_of_date", "price_date", "source", "hierarchy"})
        self.assertEqual(parameters["price_date"].default.value, "as_of_date")
        self.assertEqual(parameters["hierarchy"].default.value, "Local")
        self.assertFalse(parameters["source"].required)
        self.assertEqual(parameters["source"].type.name, "code")

    def test_fixed_binding_eliminates_parameter(self):
        analysis = self.analyze("""
            decodes Industries { - 1 Primary: 'Primary' }
            entity E {
                dimension industries { value industry: code<Industries> }
                @temporal(day: date) @dimension(industries) scd attr industry: string
                computed attr primary: string { return self.industry::value(industry=Industries.Primary) }
            }
        """)
        self.assertEqual([d.message for d in analysis.diagnostics if d.severity == "error"], [])
        self.assertEqual(
            [p.name for p in analysis.calculations[(("", "E"), "primary")].inputs],
            ["day"],
        )

    def test_cycles_and_contracts(self):
        cases = [
            (
                'entity E { computed attr a: integer { return self.b::value() } computed attr b: integer { return self.a::value() } }',
                "dependency cycle",
            ),
            (
                "entity E { @temporal(day: date) snapshot attr x: integer computed attr a: integer { return self.x::value(day=input.day ?? input.day) } }",
                "default cycle",
            ),
            (
                "entity E { computed attr a: integer { return 1 } } entity F inherits E { @override computed attr a: integer { return 2 } }",
                None,
            ),
            (
                'entity E { @temporal(day: date) snapshot attr x: integer computed attr a: integer { return self.x::value() } } entity F inherits E { @override computed attr a: integer { return 2 } }',
                "parameter signature mismatch",
            ),
            (
                "entity E { static attr a: integer } entity F inherits E { @override computed attr a: integer { return 2 } }",
                "override signature mismatch",
            ),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                errors = self.errors(source)
                if expected:
                    self.assertTrue(any(expected in e for e in errors), errors)
                else:
                    self.assertEqual(errors, [])

    def test_collection_contracts(self):
        prefix = "entity Item { static attr n: integer static attr label: string static attr children: collection<Item> } entity E { static attr items: collection<Item> "
        cases = [
            (
                "mapping<string, integer>",
                "self.items::value().filter(i -> i.n::value() > 0).group_by(i -> i.label::value()).sum_by(i -> i.n::value())",
            ),
            ("integer", "self.items::value().map(i -> i.n::value() * 2).sum_by(n -> n)"),
            (
                "integer",
                "self.items::value().flat_map(i -> i.children::value()).sum_by(i -> i.n::value())",
            ),
            ("integer", "if true then 1 else 2"),
            ("decimal", "1 + 2.5"),
            (
                "integer",
                "self.items::value().sum_by(i -> if i.n::value() > 0 then i.n::value() else 0)",
            ),
        ]
        for result, expression in cases:
            with self.subTest(expression=expression):
                self.assertEqual(
                    self.errors(
                        prefix + f"computed attr total: {result} {{ return {expression} }} }}"
                    ),
                    [],
                )

    def test_recover(self):
        source = 'entity E { static attr amount: decimal computed attr total: decimal { return self.amount::value().recover(case EMPTY -> 0, case NO_ENTRY, NO_DATA -> 0, case PARTIAL as p -> p.value, case ERROR -> -1) } }'
        self.assertEqual(self.errors(source), [])
        self.assertTrue(
            any(
                "duplicate recovery status" in e
                for e in self.errors(source.replace("NO_ENTRY, NO_DATA", "NO_ENTRY, EMPTY"))
            )
        )
        self.assertTrue(
            any(
                "incompatible recovery" in e
                for e in self.errors(source.replace("case EMPTY -> 0", "case EMPTY -> 'bad'"))
            )
        )

    def test_expression_diagnostics(self):
        prefix = "entity Item { static attr n: integer } entity E { static attr x: integer static attr items: collection<Item> "
        cases = [
            ("unknown", "unknown calculation name"),
            ("self.missing", "unknown entity field"),
            ("self.x.missing", "scalar entity reference"),
            ("self.x + 'a'", "arithmetic requires"),
            ("self.x and true", "boolean operators"),
            ("self.x == 'a'", "comparison requires"),
            ("if 1 then 2 else 3", "if condition"),
            ("if true then 1 else 'a'", "if branches"),
            ("self.x.unknown()", "unknown calculation method"),
            ("self.items.sum_by(1)", "one lambda"),
            ("self.x.sum_by(x -> x)", "requires a sequence"),
            ("self.items.sum_by(i -> true)", "numeric contributions"),
            ("self.items.filter(i -> i.n).sum_by(i -> i.n)", "predicate requires bool"),
            ("self.items.flat_map(i -> i.n).sum_by(i -> i.n)", "flat_map requires"),
            ("self.x::unsupported()", "stored fields support"),
            ("1::value()", "requires a field descriptor"),
            ("self.x::value(day=1)", "unknown or duplicate field argument"),
            ("self.x.recover()", "recover requires"),
            ("self.x.recover(case UNKNOWN -> 0)", "unknown recovery status"),
            ("self.x.recover(case EMPTY as p -> 0)", "unique PARTIAL"),
        ]
        for expression, expected in cases:
            with self.subTest(expression=expression):
                errors = self.errors(
                    prefix + f"computed attr result: integer {{ return {expression} }} }}"
                )
                self.assertTrue(any(expected in e for e in errors), errors)

    def test_additional_context_conflicts_and_invalid_default_bindings(self):
        cases = [
            (
                "entity E { computed attr x: integer { let a = 1 let a = 2 return a } }",
                "duplicate or invalid local",
            ),
            (
                "entity E { snapshot attr x: integer computed attr y: integer { return self.x::value(as_of_date=input.best_effort) } }",
                "reserved calculation parameter",
            ),
            (
                'entity E { dimension d { value as_of_date: string } @dimension(d) static attr x: integer snapshot attr y: integer computed attr z: integer { return self.x::value() + self.y::value() } }',
                "conflicting inferred parameter",
            ),
            (
                "entity E { snapshot attr x: integer computed attr y: integer { return self.x::value(as_of_date=input.day.default()) } }",
                "unknown calculation method",
            ),
            (
                "entity E { snapshot attr x: integer computed attr y: integer { return self.x::value(as_of_date=1) } }",
                "incompatible binding",
            ),
            (
                "entity E { computed attr x: integer { return 1 } computed attr y: integer { return self.x::best_available().value } }",
                "computed fields support",
            ),
            (
                "entity E { computed attr y: integer { return input.day } }",
                "unknown calculation name",
            ),
            (
                "entity E { computed attr y: integer { return self::type } }",
                "binding or method context",
            ),
            (
                'entity E { static attr items: collection<E> computed attr y: integer { return self.items::value().group_by(x -> x.items::value()).sum_by(x -> 1) } }',
                "scalar key",
            ),
            (
                'entity E { static attr items: collection<E> computed attr y: integer { return self.items::value().group_by(x -> 1).filter(x -> true).sum_by(x -> 1) } }',
                "grouped values support",
            ),
            (
                'entity E { static attr items: collection<E> computed attr y: integer { let row = 0 return self.items::value().sum_by(row -> 1) } }',
                "unique snake_case",
            ),
            (
                "entity E { dimension d { value source: string fallback_days = true } }",
                "integer expression",
            ),
            (
                'entity E { snapshot attr x: integer dimension d { value source: string fallback_days = self.x::value() } }',
                "without external parameters",
            ),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                errors = self.errors(source)
                self.assertTrue(any(expected in message for message in errors), errors)
        self.assertEqual(
            self.errors("entity E { computed attr x: decimal { return if true then 1 else 2.5 } }"),
            [],
        )
        self.assertEqual(
            self.errors(
                "entity E { dimension d { value source: string hierarchy h: Hierarchy } @dimension(d) snapshot attr x: integer computed attr y: integer { return self.x::best_available(h='Fixed').value } }"
            ),
            [],
        )
        analysis = self.analyze(
            'entity Root { computed attr x: integer { return self.only_child::value() } } entity Child inherits Root { static attr only_child: integer }'
        )
        self.assertEqual([d.message for d in analysis.diagnostics if d.severity == "error"], [])

    def test_child_default_is_rebound_and_constant_input_is_eliminated(self):
        source = """entity E {
            static attr date_value: date
            snapshot attr x: integer
            computed attr child: integer { return self.x::value(as_of_date=input.other ?? input.day) }
            computed attr parent: integer { return self.child::value(day=self.date_value::value(), other=input.valuation_date) }
        }"""
        result = self.analyze(source)
        self.assertEqual([d.message for d in result.diagnostics if d.severity == "error"], [])
        inputs = result.calculations[(("", "E"), "parent")].inputs
        self.assertEqual([p.name for p in inputs], ["valuation_date"])
        self.assertEqual(inputs[0].default.operands[0].operands[0].value, "date_value")


if __name__ == "__main__":
    unittest.main()
