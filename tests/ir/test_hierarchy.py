"""Verify shared dimensions and named hierarchies survive independent IR validation."""

import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import document
from yambl_compiler.ir.codec import dumps, loads, seal
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.validation import validate_ir
from yambl_compiler.languages.yambl.semantics.validation import analyze


class HierarchyIRTests(unittest.TestCase):
    def test_hierarchy_default_inputs_are_reconstructed_and_checked(self):
        source = '''entity E {
            @temporal(day: date) scd attr dictionary: Decodes
            dimension feed { value source: string hierarchy h: Hierarchy = self.dictionary::value(day=input.query)::hierarchy() }
            @dimension(feed) attr price: integer
        }'''
        analysis = analyze([document(source)])
        self.assertEqual(analysis.diagnostics, ())
        model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
        self.assertEqual(validate_ir(model), ())
        entity = next(item for item in model.definitions if item.identity == 'E')
        dimension = entity.dimensions[0]
        self.assertEqual(
            [(item.name, item.type.name) for item in dimension.hierarchy_inputs],
            [('query', 'date')],
        )
        altered = replace(entity, dimensions=(replace(dimension, hierarchy_inputs=()),))
        tampered = seal(
            replace(
                model,
                definitions=tuple(
                    altered if item is entity else item for item in model.definitions
                ),
            )
        )
        self.assertTrue(any('hierarchy_inputs' in message for message in validate_ir(tampered)))

    def test_local_and_shared_dimension_identities_do_not_collide(self):
        sources = [
            'namespace core.Base dimension feed { value source: string }',
            'namespace core from core.Base import feed '
            'entity Base { dimension feed { value source: string } @dimension(feed) attr x: string } '
            'entity Other { @dimension(feed) attr y: string }',
        ]
        analysis = analyze([document(source, f'{i}.yambl') for i, source in enumerate(sources)])
        self.assertEqual(analysis.diagnostics, ())
        model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
        self.assertEqual(validate_ir(model), ())
        fields = {
            field.implementation: call.dimension_identity
            for definition in model.definitions
            for field in definition.fields
            for call in field.decorators
            if call.target == 'builtins.dimension'
        }
        self.assertEqual(
            fields, {'core.Base.x': 'core.Base::dimension.feed', 'core.Other.y': 'core.Base.feed'}
        )

    def test_named_hierarchy_entries_preserve_qualified_dictionary(self):
        sources = [
            "namespace dictionaries decodes Sources { - 1 Wind: 'Wind' }",
            "namespace core from dictionaries import Sources "
            "hierarchy Preferred { - 100 Sources.Wind - 200 'Manual' }",
        ]
        analysis = analyze([document(source, f'{i}.yambl') for i, source in enumerate(sources)])
        self.assertEqual(analysis.diagnostics, ())
        model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
        self.assertEqual(validate_ir(model), ())
        definition = next(item for item in model.definitions if item.identity == 'core.Preferred')
        self.assertEqual(
            [
                (entry.ordering, entry.value, entry.dictionary)
                for entry in definition.hierarchy_entries
            ],
            [('100', 'Wind', 'dictionaries.Sources'), ('200', 'Manual', '')],
        )

    def test_recovery_default_keeps_the_declaration_scope(self):
        source = """namespace core
hierarchy H {}
entity E {
    attr dictionary: Decodes
    dimension d {
        value source: string
        hierarchy order: Hierarchy = self.dictionary::value()::hierarchy().recover(case NO_ENTRY -> H)
    }
    @dimension(d) attr label: string
    computed attr answer: string { return self.label::best_available().value }
}
"""
        analysis = analyze([document(source)])
        self.assertEqual(analysis.diagnostics, ())
        model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
        self.assertEqual(validate_ir(model), ())
        entity = next(item for item in model.definitions if item.identity == 'core.E')
        self.assertEqual(
            [(item.name, item.value) for item in entity.dimensions[0].references], [('H', 'core.H')]
        )
        answer = next(field for field in entity.fields if field.name == 'answer')
        selector = next(item for item in answer.calculation.inputs if item.name == 'order')
        self.assertEqual(selector.default_dimension, 'core.E::dimension.d')


if __name__ == '__main__':
    unittest.main()
