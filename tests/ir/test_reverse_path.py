"""Verify checked reverse path identities in standalone IR."""

import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import document
from yambl_compiler.ir.codec import dumps, loads, seal
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.validation import validate_ir
from yambl_compiler.languages.yambl.semantics.validation import analyze


class ReversePathIRTests(unittest.TestCase):
    def test_path_keeps_start_type_and_inherited_field_identity(self):
        source = '''entity A { id id: string @relationship(one, maybe) attr b: B }
entity Child inherits A {}
entity B { id id: string @relationship(one, maybe) attr c: C }
entity C { id id: string @reverse_relationship(Child.b.c) attr a: Child }'''
        analysis = analyze([document(source)])
        self.assertEqual([d for d in analysis.diagnostics if d.severity == 'error'], [])
        model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
        self.assertEqual(validate_ir(model), ())
        definition = next(d for d in model.definitions if d.identity == 'C')
        field = next(f for f in definition.fields if f.name == 'a')
        call = next(c for c in field.decorators if c.target == 'builtins.reverse_relationship')
        self.assertEqual(call.reverse_source, 'Child')
        self.assertEqual(
            [(step.entity, step.field) for step in call.reverse_path],
            [('Child', 'A.b'), ('B', 'B.c')],
        )
        changed_call = replace(call, reverse_path=call.reverse_path[:-1])
        changed_field = replace(
            field, decorators=tuple(changed_call if c is call else c for c in field.decorators)
        )
        changed_definition = replace(
            definition, fields=tuple(changed_field if f is field else f for f in definition.fields)
        )
        tampered = seal(
            replace(
                model,
                definitions=tuple(
                    changed_definition if d is definition else d for d in model.definitions
                ),
            )
        )
        self.assertTrue(any('reverse_path' in issue for issue in validate_ir(tampered)))
