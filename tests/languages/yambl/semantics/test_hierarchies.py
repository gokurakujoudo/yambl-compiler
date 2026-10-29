"""Check shared dimensions, named Hierarchies and Decodes views."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.formatting.formatter import format_document
from yambl_compiler.languages.yambl.semantics.dimensions import field_dimension
from yambl_compiler.languages.yambl.semantics.hierarchies import hierarchy_entries
from yambl_compiler.languages.yambl.semantics.validation import analyze
from yambl_compiler.languages.yambl.syntax.parser import annotations, children


class HierarchyTests(unittest.TestCase):
    def test_shared_scope_order_and_entries(self):
        common = document(
            """
            namespace shared
            decodes Sources { - 1 Wind: 'Wind' - 2 Manual: 'Manual' }
            dimension feed { value source: code<Sources> hierarchy priority: Hierarchy = Sources::hierarchy() }
            hierarchy Preferred { - 10 Sources.Wind - 20 'Manual' - 30 Sources.Wind }
            hierarchy Empty {}
        """,
            'shared.yambl',
        )
        use = document(
            """
            namespace core
            from shared import feed
            from shared import Preferred
            entity E {
                id key: string
                @dimension(feed) static attr shared: string
                dimension local { value channel: string }
                @dimension(local) static attr own: string
                computed attr selected: string { return self.shared::best_available(priority=Preferred) }
            }
        """,
            'core.yambl',
        )
        analysis = analyze([common, use])
        self.assertEqual([d.message for d in analysis.diagnostics], [])
        shared = analysis.units[0]
        declaration = children(shared.document.tree, 'hierarchy')[0]
        self.assertEqual(
            [
                (entry.ordering, entry.value, entry.dictionary)
                for entry in hierarchy_entries(shared, declaration, [])
            ],
            [
                (10, 'Wind', ('shared', 'Sources')),
                (20, 'Manual', None),
                (30, 'Wind', ('shared', 'Sources')),
            ],
        )
        self.assertEqual(
            hierarchy_entries(shared, children(shared.document.tree, 'hierarchy')[1], []), ()
        )
        entity = children(analysis.units[1].document.tree, 'entity')[0]
        fields = children(entity, 'field')
        resolved = [
            field_dimension(analysis.units[1], field, annotations(field, 'dimension')[0], [])
            for field in fields[1:3]
        ]
        self.assertEqual(
            [item.identity for item in resolved],
            [('shared', 'feed'), ('core', 'E::dimension.local')],
        )
        formatted = format_document(common)
        self.assertEqual(format_document(document(formatted)), formatted)
        self.assertIn("    - 20 'Manual'\n", formatted)

    def test_local_dimension_shadows_shared_and_import_scope_is_per_file(self):
        documents = [
            document('namespace common dimension feed { value shared: string }'),
            document(
                'namespace core from common import feed entity Base { dimension feed { value local: string } @dimension(feed) attr value: string } entity Child inherits Base { @dimension(feed) attr other: string }'
            ),
            document('namespace core entity Missing { @dimension(feed) attr value: string }'),
        ]
        analysis = analyze(documents)
        self.assertEqual([d.message for d in analysis.diagnostics], ['unknown dimension: feed'])
        entity = children(analysis.units[1].document.tree, 'entity')[1]
        field = children(entity, 'field')[0]
        selected = field_dimension(analysis.units[1], field, annotations(field, 'dimension')[0], [])
        self.assertEqual(selected.identity, ('core', 'Base::dimension.feed'))

    def test_shared_namespace_and_local_entity_names_do_not_collide(self):
        documents = [
            document('namespace core.Base dimension feed { value source: string }'),
            document(
                'namespace core entity Base { dimension feed { value source: string } @dimension(feed) attr x: string }'
            ),
            document(
                'namespace consumer from core.Base import feed entity Use { @dimension(feed) attr x: string }'
            ),
        ]
        analysis = analyze(documents)
        self.assertEqual([item.message for item in analysis.diagnostics], [])
        identities = []
        for unit in analysis.units[1:]:
            entity = children(unit.document.tree, 'entity')[0]
            field = children(entity, 'field')[0]
            identities.append(
                field_dimension(unit, field, annotations(field, 'dimension')[0], []).identity
            )
        self.assertEqual(identities, [('core', 'Base::dimension.feed'), ('core.Base', 'feed')])

    def test_invalid_hierarchy_entries_and_shared_context(self):
        cases = [
            ('hierarchy Bad { - 1 \'a\' - 1 \'b\' }', 'duplicate Hierarchy ordering'),
            ('hierarchy bad {}', 'declaration names must use PascalCase'),
            ('hierarchy Bad { - 1 Missing.X }', 'unknown Decodes: Missing'),
            (
                "decodes D { - 1 X: 'x' } hierarchy Bad { - 1 D.Missing }",
                'unknown Decodes code: D.Missing',
            ),
            (
                "@flags decodes F { - 1 X: 'x' } hierarchy Bad { - 1 F.X }",
                'Hierarchy entries require ordinary Decodes',
            ),
            (
                "@flags decodes F { - 1 X: 'x' } dimension d { value source: string hierarchy h: Hierarchy = F::hierarchy() }",
                'flags Decodes cannot be converted to Hierarchy',
            ),
            (
                "decodes D { - 1 X: 'x' } dimension d { value source: string hierarchy h: Hierarchy = D::hierarchy(1) }",
                '::hierarchy() does not accept arguments',
            ),
            ('dimension Bad { value x: string }', 'dimension names must use snake_case'),
            ('dimension d { value Bad: string }', 'dimension names must use snake_case'),
            (
                'dimension d { value x: string } hierarchy d {}',
                'duplicate or reserved declaration: d',
            ),
            (
                "const H: string = 'Local' dimension d { value x: string hierarchy h: Hierarchy = H }",
                'hierarchy default requires a Hierarchy reference or view',
            ),
            (
                'dimension d { value x: string hierarchy h: Hierarchy = EMPTY }',
                'hierarchy default requires a Hierarchy reference or view',
            ),
            (
                "dimension d { value x: string hierarchy h: Hierarchy = if true then EMPTY else 'Local' }",
                'hierarchy default requires a Hierarchy reference or view',
            ),
            (
                'dimension d { value x: string fallback_days = self.days::value() }',
                'shared dimension cannot use self',
            ),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertIn(
                    expected, [d.message for d in analyze([document(source)]).diagnostics]
                )

    def test_dictionary_conversion_types_and_flags(self):
        source = """
            decodes Sources { - 1 Wind: 'Wind' }
            @flags decodes Flags { - 1 One: 'One' }
            hierarchy Preferred { - 1 Sources.Wind }
            entity E {
                id key: string
                static attr dictionary: Decodes
                dimension d { value source: string hierarchy h: Hierarchy = self.dictionary::value()::hierarchy() }
                @dimension(d) static attr value: string
                computed attr good: string { return self.value::best_available(h=Sources::hierarchy()) }
                computed attr named: string { return self.value::best_available(h=Preferred) }
                computed attr bad: string { return self.value::best_available(h=Flags::hierarchy()) }
                computed attr wrong: string { return self.value::best_available(h='x'::hierarchy()) }
            }
        """
        messages = [d.message for d in analyze([document(source)]).diagnostics]
        self.assertIn('flags Decodes cannot be converted to Hierarchy', messages)
        self.assertIn('::hierarchy() requires a Decodes reference', messages)
        self.assertEqual(len(messages), 2, messages)

    def test_default_dependencies_and_explicit_bindings(self):
        source = """
            hierarchy Preferred { - 1 'Feed' }
            entity E {
                id key: string
                @temporal(day: date) scd attr dictionary: Decodes
                dimension d { value source: string hierarchy h: Hierarchy = self.dictionary::value()::hierarchy() }
                @dimension(d) @temporal(day: date) snapshot attr label: string
                @dimension(d) static attr static_label: string
                computed attr plain: string { return self.label::best_available() }
                computed attr renamed: string { return self.label::best_available(day=input.query) }
                computed attr fixed: string { return self.label::best_available(day=parse_date('2026-10-29'), h=Preferred) }
                computed attr direct: string { return self.label::best_available(source='Feed') }
                computed attr fallback: string { return self.label::best_available(h=input.choice ?? Preferred) }
                @extra_input_param(h: Hierarchy = 'Preferred')
                computed attr declared: string { return self.label::best_available() }
                computed attr static_direct: string { return self.static_label::best_available(source='Feed') }
                computed attr static_fixed: string { return self.static_label::best_available(h=Preferred) }
                computed attr static_default: string { return self.static_label::best_available() }
                computed attr aliased: string { return self.static_label::best_available(h=input.choice) }
                @extra_input_param(choice: Hierarchy = 'Preferred')
                computed attr alias_declared: string { return self.static_label::best_available(h=input.choice) }
                computed attr nested: string { return self.plain::value(day=input.nested_day) }
            }
        """
        analysis = analyze([document(source)])
        self.assertEqual([item.message for item in analysis.diagnostics], [])
        contracts = {
            name: {item.name: item for item in calculation.inputs}
            for (_, name), calculation in analysis.calculations.items()
        }
        self.assertEqual(set(contracts['plain']), {'day', 'h', 'source'})
        self.assertTrue(contracts['plain']['day'].required)
        self.assertEqual(contracts['plain']['day'].type.name, 'date')
        self.assertEqual(contracts['plain']['h'].default_dimension, ('', 'E::dimension.d'))
        self.assertEqual(set(contracts['renamed']), {'query', 'h', 'source'})
        self.assertNotIn('day', contracts['fixed'])
        self.assertNotIn('source', contracts['direct'])
        self.assertNotIn('day', contracts['static_direct'])
        self.assertNotIn('day', contracts['static_fixed'])
        self.assertFalse(contracts['static_default']['day'].required)
        self.assertIn('day', contracts['aliased'])
        self.assertFalse(contracts['aliased']['day'].required)
        self.assertEqual(contracts['aliased']['choice'].default_dimension, ('', 'E::dimension.d'))
        self.assertNotIn('day', contracts['alias_declared'])
        self.assertEqual(set(contracts['nested']), {'nested_day', 'h', 'source'})
        self.assertEqual(contracts['nested']['h'].default_dimension, ('', 'E::dimension.d'))
        self.assertIsNone(contracts['fallback']['choice'].default_dimension)
        self.assertIsNone(contracts['declared']['h'].default_dimension)

    def test_default_dependency_can_rename_its_input(self):
        source = """
            entity E {
                id key: string
                @temporal(day: date) scd attr dictionary: Decodes
                dimension d { value source: string hierarchy h: Hierarchy = self.dictionary::value(day=input.history_day)::hierarchy() }
                @dimension(d) static attr label: string
                computed attr result: string { return self.label::best_available() }
            }
        """
        analysis = analyze([document(source)])
        self.assertEqual([item.message for item in analysis.diagnostics], [])
        inputs = {item.name: item for item in analysis.calculations[(('', 'E'), 'result')].inputs}
        self.assertEqual(set(inputs), {'h', 'history_day', 'source'})
        self.assertFalse(inputs['history_day'].required)
        self.assertEqual(inputs['history_day'].type.name, 'date')

    def test_same_input_defaults_compare_their_lexical_declarations(self):
        one = "namespace one hierarchy H { - 1 'A' } dimension d_one { value source:string hierarchy h:Hierarchy=H }"
        two = "namespace two hierarchy H { - 1 'B' } dimension d_two { value source:string hierarchy h:Hierarchy=H }"
        use = "namespace use from one import d_one from two import d_two entity E { id id:string @dimension(d_one) attr x:string @dimension(d_two) attr y:string computed attr z:bool { return self.x::best_available() == self.y::best_available() } }"
        result = analyze([document(one, 'one'), document(two, 'two'), document(use, 'use')])
        self.assertIn(
            'conflicting inferred parameter: h', [item.message for item in result.diagnostics]
        )
        equivalent = two.replace("hierarchy H { - 1 'B' }", 'from one import H')
        result = analyze([document(one, 'one'), document(equivalent, 'two'), document(use, 'use')])
        self.assertEqual([item.message for item in result.diagnostics], [])
        literals = [
            one.replace('hierarchy h:Hierarchy=H', "hierarchy h:Hierarchy='same'"),
            two.replace('hierarchy h:Hierarchy=H', "hierarchy h:Hierarchy='same'"),
            use,
        ]
        result = analyze([document(item, str(index)) for index, item in enumerate(literals)])
        self.assertEqual([item.message for item in result.diagnostics], [])

    def test_local_default_references_preserve_expression_scope(self):
        source = """
            namespace core
            hierarchy H { - 1 'A' }
            entity E {
                id id: string
                @temporal(day: date) scd attr dictionary: Decodes
                dimension a { value source: string hierarchy h: Hierarchy = self.dictionary::value(day=input.query)::hierarchy().recover(case NO_ENTRY -> H) }
                dimension b { value source: string hierarchy h: Hierarchy = self.dictionary::value(day=input.query)::hierarchy().recover(case NO_ENTRY -> H) }
                @dimension(a) attr x: string
                @dimension(b) attr y: string
                computed attr z: bool { return self.x::best_available() == self.y::best_available() }
            }
        """
        result = analyze([document(source)])
        self.assertEqual([item.message for item in result.diagnostics], [])
        inputs = {item.name: item for item in result.calculations[(('core', 'E'), 'z')].inputs}
        self.assertEqual(inputs['h'].default_dimension, ('core', 'E::dimension.a'))
        self.assertFalse(inputs['query'].required)

        separate = """
            namespace core
            entity A { id id: string attr dictionary: Decodes dimension a { value source: string hierarchy h: Hierarchy = self.dictionary::value()::hierarchy() } @dimension(a) attr x: string }
            entity B { id id: string attr dictionary: Decodes dimension b { value source: string hierarchy h: Hierarchy = self.dictionary::value()::hierarchy() } @dimension(b) attr x: string }
            entity T { id id: string attr a: A attr b: B computed attr z: bool { return self.a::value().x::best_available() == self.b::value().x::best_available() } }
        """
        result = analyze([document(separate)])
        self.assertIn(
            'conflicting inferred parameter: h', [item.message for item in result.diagnostics]
        )


if __name__ == '__main__':
    unittest.main()
