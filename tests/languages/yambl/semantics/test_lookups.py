"""Named lookup path and preparation parameter contracts."""

import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from tests.support import document
from yambl_compiler.ir.codec import dumps, loads
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.languages.yambl.semantics.builtins import DOCUMENTS, RESULT_CONSTANTS
from yambl_compiler.languages.yambl.semantics.symbols import resolve_scopes
from yambl_compiler.languages.yambl.semantics.validation import analyze, analyze_units
from yambl_compiler.storage.layout import build_layout


class LookupTests(unittest.TestCase):
    def test_embedded_dimensions_and_extension_survive_analysis_and_ir(self):
        original = next(item for item in DOCUMENTS if item.path.name == 'hierarchy.yambl')
        source = original.text.replace(
            'namespace builtins', "namespace builtins\nconst SOURCE: string = 'feed'", 1
        ).replace(
            'id hierarchy_id: string',
            "id hierarchy_id: string\n dimension d { value source: string }\n @dimension(d) @temporal(day: date) scd attr label: string\n lookup by_label { key=self.label::value(day=input.query, source=SOURCE) }",
            1,
        )
        hierarchy = replace(document(source, str(original.path)), builtin=True)
        open_entity = replace(
            document(
                'namespace builtins entity BuiltinOpen { id key: string }', 'builtin-open.yambl'
            ),
            builtin=True,
        )
        extension = replace(
            document(
                'namespace builtins extend BuiltinOpen { lookup by_id { key = self.key::value() } }',
                'builtin-extension.yambl',
            ),
            builtin=True,
        )
        builtins = [hierarchy if item.path == original.path else item for item in DOCUMENTS]
        builtins.extend((open_entity, extension))
        partial_override = [
            document(
                'namespace builtins value hierarchy_entry { attr entry_value: string attr ordering: integer }'
            ),
            document('entity E { id id: string }'),
        ]
        for inputs in ([document('entity E { id id: string }')], builtins, partial_override):
            with self.subTest(builtin_repository=inputs is builtins):
                diagnostics = []
                analysis = analyze_units(
                    resolve_scopes(inputs, diagnostics, builtins, RESULT_CONSTANTS), diagnostics
                )
                self.assertEqual(analysis.diagnostics, ())
                lookup = analysis.lookups[('builtins', 'Hierarchy')]['by_label']
                self.assertEqual(
                    [(item.name, item.type.name) for item in lookup.inputs], [('query', 'date')]
                )
                self.assertEqual(
                    next(
                        item.value
                        for item in lookup.keys[0].steps[0].parameters
                        if item.requirement.name == 'source'
                    ),
                    'feed',
                )
                self.assertEqual(
                    analysis.lookups[('builtins', 'BuiltinOpen')]['by_id'].keys[0].type.name,
                    'string',
                )
                model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
                hierarchy_ir = next(
                    item for item in model.definitions if item.identity == 'builtins.Hierarchy'
                )
                self.assertEqual(
                    [(item.name, item.type.name) for item in hierarchy_ir.lookups[0].inputs],
                    [('query', 'date')],
                )
                open_ir = next(
                    item for item in model.definitions if item.identity == 'builtins.BuiltinOpen'
                )
                self.assertEqual(open_ir.lookups[0].name, 'by_id')
                with self.assertRaisesRegex(ValueError, 'builtin entity storage is not supported'):
                    build_layout(model)
        for altered, error in (
            (
                hierarchy,
                'builtin storage does not support temporal, config, computed or dimensioned fields',
            ),
            (
                replace(
                    document(
                        original.text.replace(
                            'id hierarchy_id: string',
                            'id hierarchy_id: string\n static attr label: string',
                            1,
                        ),
                        str(original.path),
                    ),
                    builtin=True,
                ),
                'builtin storage does not support additional fields',
            ),
            (
                replace(
                    document(
                        original.text.replace(
                            'id hierarchy_id: string',
                            'id hierarchy_id: string\n static attr extra: integer',
                            1,
                        ),
                        str(original.path),
                    ),
                    builtin=True,
                ),
                'builtin storage does not support additional fields',
            ),
            (
                replace(
                    document(
                        original.text.replace(
                            'static attr hierarchy_entries: values<hierarchy_entry>',
                            'static attr hierarchy_entries: string',
                            1,
                        ),
                        str(original.path),
                    ),
                    builtin=True,
                ),
                'builtin storage does not support field type string',
            ),
        ):
            with self.subTest(error=error):
                documents = [altered if item.path == original.path else item for item in DOCUMENTS]
                diagnostics = []
                analysis = analyze_units(
                    resolve_scopes(
                        [document('entity E { id id: string }')],
                        diagnostics,
                        documents,
                        RESULT_CONSTANTS,
                    ),
                    diagnostics,
                )
                self.assertEqual(analysis.diagnostics, ())
                model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
                with self.assertRaisesRegex(ValueError, error):
                    build_layout(model)
        invalid = replace(
            document(
                'namespace builtins extend Missing { lookup bad { x=self.x::value() } }',
                'invalid-builtin-extension.yambl',
            ),
            builtin=True,
        )
        diagnostics = []
        analysis = analyze_units(
            resolve_scopes(
                [document('entity E { id id: string }')],
                diagnostics,
                [*builtins, invalid],
                RESULT_CONSTANTS,
            ),
            diagnostics,
        )
        self.assertIn(
            'extend requires a visible user-defined entity: Missing',
            [item.message for item in analysis.diagnostics],
        )
        diagnostics = []
        business = document('const OUT: string = SOURCE entity E { id id: string }')
        analysis = analyze_units(
            resolve_scopes([business], diagnostics, builtins, RESULT_CONSTANTS), diagnostics
        )
        self.assertIn(
            'unknown calculation name: SOURCE', [item.message for item in analysis.diagnostics]
        )

    def test_empty_compilation_has_no_lookup_rules(self):
        analysis = analyze([])
        self.assertEqual(analysis.diagnostics, ())
        self.assertEqual(analysis.lookups, {})

    def test_invalid_rules_and_failed_constant(self):
        cases = (
            (
                'entity E { attr x:string lookup Bad { x=self.x::value() } }',
                'lookup names must use snake_case',
            ),
            (
                'extend Missing { lookup a { x=self.x::value() } }',
                'extend requires a visible user-defined entity: Missing',
            ),
            (
                'entity E { @temporal(day:date) scd attr x:string lookup a { x=self.x::value(day=false) } }',
                'incompatible binding for parameter: day',
            ),
            (
                "const BAD: date = parse_date('bad') entity E { @temporal(day:date) scd attr x:string lookup a { x=self.x::value(day=BAD) } }",
                'cannot evaluate lookup binding: BAD',
            ),
            (
                "entity E { attr x:string lookup a { x=self.x::value() + 'x' } }",
                'lookup key requires a self field path ending in ::value()',
            ),
            (
                'entity E { attr target:type<Entity> attr selected:self.target::value lookup a { x=self.selected::value() } }',
                'lookup key requires a supported non-collection type',
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertIn(
                    expected, [item.message for item in analyze([document(source)]).diagnostics]
                )
        invalid = analyze(
            [document('entity E { lookup a { x=self.missing::value().y::value() } }')]
        )
        self.assertEqual(
            sum(
                item.message == 'field operation requires a field descriptor'
                for item in invalid.diagnostics
            ),
            1,
        )

    def test_compatible_inputs_merge_independently_of_order(self):
        source = """decodes D { - 1 X: 'X' } entity E {
            dimension a { value choice:string } dimension b { value choice:code<D> }
            @dimension(a) attr x:string @dimension(b) attr y:string
            lookup by_x { x=self.x::value() y=self.y::value() }
        }"""
        result = analyze([document(source)])
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.lookups[('', 'E')]['by_x'].inputs[0].type.args[0].key, ('', 'D'))

    def test_analyze_units_retains_checked_rules(self):
        source = 'entity E { attr name: string lookup by_name { key = self.name::value() } }'
        diagnostics = []
        units = resolve_scopes([document(source)], diagnostics)
        analysis = analyze_units(units, diagnostics)
        self.assertEqual(analysis.diagnostics, ())
        self.assertEqual(analysis.lookups[('', 'E')]['by_name'].keys[0].type.name, 'string')

    def test_paths_and_independent_parameter_bindings(self):
        analysis = analyze([document("""
            decodes Sources { - 1 Primary: 'Primary' }
            entity Issuer {
                id id: string
                dimension industries { value source: code<Sources> }
                @dimension(industries) @temporal(industry_date: date) scd attr industry: string
                static attr name: string
            }
            entity Security {
                id id: string
                @temporal(relation_date: date) scd attr issuer: Issuer
                lookup by_industry {
                    name = self.issuer::value(relation_date=input.date).name::value()
                    industry = self.issuer::value(relation_date=input.date).industry::value(source=Sources.Primary)
                }
            }
            entity Bond inherits Security {}
        """)])
        self.assertEqual([d.message for d in analysis.diagnostics], [])
        rule = analysis.lookups[('', 'Security')]['by_industry']
        self.assertIs(analysis.lookups[('', 'Bond')]['by_industry'], rule)
        self.assertEqual(
            [(k.name, k.type.name) for k in rule.keys], [('name', 'string'), ('industry', 'string')]
        )
        self.assertEqual(
            [(p.name, p.type.name, p.required) for p in rule.inputs],
            [('date', 'date', True), ('industry_date', 'date', True)],
        )
        self.assertEqual(
            [[a.name for a in key.accesses] for key in rule.keys],
            [['issuer', 'name'], ['issuer', 'industry']],
        )
        issuer, industry = rule.keys[1].steps
        self.assertEqual(issuer.access.owner, ('', 'Security'))
        self.assertEqual(
            [
                (p.requirement.name, p.binding.value if p.binding else None)
                for p in issuer.parameters
            ],
            [('relation_date', 'date')],
        )
        self.assertEqual(
            [
                (p.requirement.name, p.binding.value if p.binding else None)
                for p in industry.parameters
            ],
            [('industry_date', None), ('source', 'Primary')],
        )
        self.assertEqual(issuer.parameters[0].input_name, 'date')
        self.assertEqual(industry.parameters[0].input_name, 'industry_date')
        self.assertEqual(industry.parameters[1].value, 'Primary')

    def test_fixed_date_does_not_remove_another_nodes_input(self):
        analysis = analyze([document("""
            const DAY: date = parse_date('2026-10-29')
            entity Child { @temporal(event_date: date) scd attr label: string }
            entity Root {
                @temporal(event_date: date) scd attr child: Child
                lookup by_label { label = self.child::value(event_date=DAY).label::value() }
            }
        """)])
        rule = analysis.lookups[('', 'Root')]['by_label']
        self.assertEqual([p.name for p in rule.inputs], ['event_date'])
        first, second = rule.keys[0].steps
        self.assertEqual(first.parameters[0].value, date(2026, 10, 29))
        self.assertEqual(first.parameters[0].input_name, '')
        self.assertEqual(second.parameters[0].input_name, 'event_date')

    def test_declaration_conflicts_and_invalid_paths(self):
        analysis = analyze([document("""
            entity Base {
                static attr x: string
                config attr config_value: string
                lookup bad {} lookup bad { Bad = self.config_value::value() Bad = self.x::value() }
            }
            extend Base { lookup bad { key = self.x::best_available() } }
            entity A inherits Base { lookup bad { key = self.x::value() } }
        """)])
        messages = [d.message for d in analysis.diagnostics]
        for expected in (
            'lookup requires at least one key',
            'duplicate lookup declaration: bad',
            'lookup key names must use snake_case',
            'duplicate lookup key: Bad',
            'lookup does not support config fields',
            'lookup key requires a self field path ending in ::value()',
            'lookup redeclares inherited rule: bad',
        ):
            self.assertIn(expected, messages)

    def test_conflicting_inputs_and_fixed_bindings(self):
        analysis = analyze([document("""
            decodes D { - 1 One: 'One' }
            entity E {
                dimension d { value choice: code<D> }
                @dimension(d) static attr x: string
                @temporal(choice: date) scd attr y: string
                lookup conflict { x = self.x::value() y = self.y::value() }
                lookup fixed { x = self.x::value(choice=D.One) y = self.y::value(choice='2026-10-29') }
                lookup bad { x = self.x::value(choice=D.Missing) y = self.y::value(choice=self.y::value()) }
            }
        """)])
        messages = [d.message for d in analysis.diagnostics]
        self.assertIn('conflicting inferred parameter: choice', messages)
        self.assertIn(
            'lookup binding requires a pure expression of inputs, literals, constants or dictionary members',
            messages,
        )
        self.assertTrue(any('Missing' in message for message in messages))
        self.assertEqual(analysis.lookups[('', 'E')]['fixed'].inputs, ())

    def test_declared_optional_inputs_and_pure_bindings(self):
        analysis = analyze([document("""
            entity E {
                @temporal(day: date) scd attr name: string
                @extra_input_param(industry_date: date, 'Optional industry date')
                lookup by_name {
                    name = self.name::value(day=input.industry_date ?? input.as_of_date)
                }
            }
        """)])
        self.assertEqual([item.message for item in analysis.diagnostics], [])
        rule = analysis.lookups[('', 'E')]['by_name']
        self.assertEqual(
            [(item.name, item.type.name, item.required) for item in rule.inputs],
            [('as_of_date', 'date', True), ('industry_date', 'date', False)],
        )
        self.assertEqual(rule.keys[0].steps[0].parameters[0].binding.value, '??')

    def test_lookup_decorator_rejects_legacy_and_wrong_target(self):
        analysis = analyze([document("""
            entity E {
                @extra_input_params(day: date)
                lookup old { x = self.name::value() }
                @temporal(day: date)
                lookup wrong { x = self.name::value() }
                attr name: string
            }
        """)])
        messages = [item.message for item in analysis.diagnostics]
        self.assertIn('use @extra_input_param instead of @extra_input_params', messages)
        self.assertIn('decorator temporal does not allow this target', messages)
        declared = analyze([document("""
            @targets(DecoratorTargets.EntityAttr)
            decorator @extra_input_params()
            entity E { @extra_input_params attr x: string }
        """)])
        self.assertEqual(
            [item.message for item in declared.diagnostics].count(
                'use @extra_input_param instead of @extra_input_params'
            ),
            2,
        )

    def test_conditional_and_boolean_input_types(self):
        analysis = analyze([document("""
            entity E {
                @temporal(day: date) scd attr x: string
                lookup by_x {
                    x = self.x::value(day=if input.first and not input.second then input.start else input.end)
                }
            }
        """)])
        self.assertEqual([item.message for item in analysis.diagnostics], [])
        self.assertEqual(
            [(item.name, item.type.name) for item in analysis.lookups[('', 'E')]['by_x'].inputs],
            [('end', 'date'), ('first', 'bool'), ('second', 'bool'), ('start', 'date')],
        )

    def test_pure_calls_arithmetic_and_invalid_bindings(self):
        analysis = analyze([document("""
            entity E {
                dimension source { value index: string }
                @dimension(source) @temporal(day: date) scd attr name: string
                @extra_input_param(offset: integer)
                lookup by_name {
                    name = self.name::value(day=parse_date(input.text), index=to_string(input.offset + 1))
                }
                lookup fixed {
                    name = self.name::value(day=parse_date('2026-10-29'), index=to_string(1 + 2))
                }
            }
        """)])
        messages = [item.message for item in analysis.diagnostics]
        self.assertEqual(messages, [])
        rule = analysis.lookups[('', 'E')]['by_name']
        self.assertEqual(
            [(item.name, item.type.name) for item in rule.inputs],
            [('offset', 'integer'), ('text', 'string')],
        )
        fixed = analysis.lookups[('', 'E')]['fixed'].keys[0].steps[0].parameters
        self.assertEqual([item.value for item in fixed], [date(2026, 10, 29), '3'])

    def test_supported_types_and_branch_names(self):
        analysis = analyze([document("""
            @flags decodes Flags { - 1 One: 'One' }
            value details { attr label: string }
            entity Root { id id: string }
            entity Left inherits Root {
                attr flag: code<Flags>
                attr data: details
                attr price: float
                lookup by_key { flag = self.flag::value() data = self.data::value() price = self.price::value() }
            }
            entity Right inherits Root { lookup by_key { id = self.id::value() } }
        """)])
        messages = [d.message for d in analysis.diagnostics]
        self.assertEqual(messages.count('lookup key requires a supported non-collection type'), 3)
        self.assertNotIn('lookup redeclares inherited rule: by_key', messages)
        self.assertNotEqual(
            analysis.lookups[('', 'Left')]['by_key'].owner,
            analysis.lookups[('', 'Right')]['by_key'].owner,
        )

    def test_paths_do_not_resolve_sibling_only_fields(self):
        analysis = analyze([document("""
            entity Root {}
            entity Left inherits Root { lookup by_extra { key = self.extra::value() } }
            entity Right inherits Root { attr extra: string }
        """)])
        self.assertIn('unknown entity field: extra', [d.message for d in analysis.diagnostics])

    def test_overload_paths_use_common_contract(self):
        analysis = analyze([document("""
            decodes D1 { - 1 One: 'One' } decodes D2 { - 1 Two: 'Two' }
            entity Parent {} entity One inherits Parent {} entity Two inherits Parent {}
            entity Base { lookup by_target { target = self.target::value() }
                          lookup by_label { label = self.label::value() } }
            entity A inherits Base { @overload attr target: One @overload attr label: code<D1>
                                     lookup by_own_target { target = self.target::value() } }
            entity AChild inherits A {}
            entity B inherits Base { @overload attr target: Two @overload attr label: code<D2> }
        """)])
        rules = analysis.lookups[('', 'Base')]
        self.assertEqual(rules['by_target'].keys[0].type.key, ('', 'Parent'))
        self.assertEqual(rules['by_label'].keys[0].type.name, 'string')
        self.assertEqual(analysis.lookups[('', 'A')]['by_own_target'].keys[0].type.key, ('', 'One'))
        self.assertIs(
            analysis.lookups[('', 'AChild')]['by_own_target'],
            analysis.lookups[('', 'A')]['by_own_target'],
        )
        self.assertEqual([d.message for d in analysis.diagnostics], [])

    def test_constrained_string_binding_uses_existing_literal_rules(self):
        for literal in ('GOOD', 'bad'):
            with self.subTest(literal=literal):
                source = (
                    "entity A { dimension d { value source: string['[A-Z]+'] } @dimension(d) attr x: string lookup a { x = self.x::value(source='"
                    + literal
                    + "') } }"
                )
                analysis = analyze([document(source)])
                self.assertEqual([d.message for d in analysis.diagnostics], [])
                self.assertEqual(
                    analysis.lookups[('', 'A')]['a'].keys[0].steps[0].parameters[0].value, literal
                )

    def test_imported_fixed_code_and_compatible_parameter(self):
        analysis = analyze(
            [
                document("namespace dictionaries decodes D { - 1 One: 'One' }", 'dictionary.yambl'),
                document(
                    """
                namespace core
                from dictionaries import D
                entity E {
                    dimension codes { value choice: code<D> }
                    dimension names { value choice: string }
                    @dimension(codes) attr code_value: string
                    @dimension(names) attr name_value: string
                    lookup by_key { a = self.code_value::value() b = self.name_value::value() }
                    lookup fixed { a = self.code_value::value(choice=D.One) }
                }
            """,
                    'core.yambl',
                ),
            ]
        )
        errors = [d.message for d in analysis.diagnostics]
        self.assertEqual(errors, [])
        rules = analysis.lookups[('core', 'E')]
        self.assertEqual(rules['by_key'].inputs[0].type.args[0].key, ('dictionaries', 'D'))
        fixed = rules['fixed'].keys[0].steps[0].parameters[0]
        self.assertEqual(fixed.value, 'One')
        self.assertEqual(fixed.requirement.type.args[0].key, ('dictionaries', 'D'))


if __name__ == '__main__':
    unittest.main()
