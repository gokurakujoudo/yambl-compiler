"""Verify finite values in portable IR and reject tampered constraints."""

from dataclasses import replace

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository, validate_ir
from yambl_compiler.ir.codec import dumps, loads, seal


class FiniteIRTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")

    def test_normalization_round_trip_and_tampering(self):
        source = """value point { attr x: integer }
const P: point = {x: 1}
const Q: point = {x: 2}
const START: datetime = parse_datetime('2026-01-01T08:00:00+08:00')
decodes D { - 1 One: 'one' - 2 Two: 'two' }
entity A {
    id id: string
    attr amount: one_of[2, 1 + 2]
    attr location: one_of[P, Q]
    attr moment: one_of[START, parse_datetime('2026-01-02T00:00:00Z')]
    attr code: one_of[D.One, D.Two]
    attr fixed: exact<1 + 2>
    attr initial: exact<START>
    attr point: exact<P>
    attr truth: exact<1 > 0>
}
"""
        self.write('model.yambl', source)
        model = compile_repository(self.root)
        self.assertEqual(validate_ir(loads(dumps(model))), ())
        entity = next(item for item in model.definitions if item.identity == 'A')
        field = next(item for item in entity.fields if item.name == 'amount')
        self.assertEqual([item.text for item in field.type.allowed_values], ['2', '3'])
        fixed = next(item for item in entity.fields if item.name == 'fixed')
        self.assertEqual((fixed.type.exact.kind, fixed.type.exact.text), ('integer', '3'))
        self.write('model.yambl', source.replace('one_of[2, 1 + 2]', 'one_of[1 + 2, 2]'))
        self.assertEqual(compile_repository(self.root).model_digest, model.model_digest)
        self.write('model.yambl', 'unavailable')
        self.assertEqual(validate_ir(loads(dumps(model))), ())
        bad_type = replace(field.type, allowed_values=field.type.allowed_values[:1])
        changed = replace(field, type=bad_type)
        bad_entity = replace(
            entity, fields=tuple(changed if item == field else item for item in entity.fields)
        )
        damaged = seal(
            replace(
                model,
                definitions=tuple(
                    bad_entity if item == entity else item for item in model.definitions
                ),
            )
        )
        self.assertIn('invalid finite constraint', '\n'.join(validate_ir(damaged)))

    def test_finite_local_annotation_restores_constant_references(self):
        source = 'const LOW: integer = 2 entity A { id id: string computed attr number: integer { let x: one_of[LOW, 3] = 2 return x } }'
        self.write('model.yambl', source)
        model = compile_repository(self.root)
        self.write('model.yambl', source.replace('one_of[LOW, 3]', 'one_of[3, LOW]'))
        self.assertEqual(compile_repository(self.root).model_digest, model.model_digest)
        self.write('model.yambl', 'unavailable')
        self.assertEqual(validate_ir(loads(dumps(model))), ())

    def test_exact_local_annotation_restores_constant_reference(self):
        source = 'const LOW: integer = 2 entity A { id id: string computed attr number: integer { let x: exact<LOW> = 2 return x } }'
        self.write('model.yambl', source)
        model = compile_repository(self.root)
        self.write('model.yambl', 'unavailable')
        self.assertEqual(validate_ir(loads(dumps(model))), ())

    def test_exact_flags_keep_existing_set_semantics(self):
        source = """@flags decodes D { - 1 One: 'one' - 2 Two: 'two' }
entity A {
    id id: string
    attr flags: exact<D.One + D.Two>
    attr options: one_of[D.One, D.All]
    computed attr value: code<D> { let x: exact<D.All> = D.All return x }
}
"""
        self.write('model.yambl', source)
        model = compile_repository(self.root)
        self.write('model.yambl', 'unavailable')
        self.assertEqual(validate_ir(loads(dumps(model))), ())
        self.write(
            'model.yambl', source.replace('one_of[D.One, D.All]', 'one_of[D.All, D.One + D.Two]')
        )
        with self.assertRaisesRegex(ValueError, 'two distinct'):
            compile_repository(self.root)
