"""Verify finite values in portable IR and reject tampered constraints."""

from dataclasses import replace

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository, validate_ir
from yambl_compiler.ir.codec import dumps, loads, seal
from yambl_compiler.ir.model import Text, Value
from yambl_compiler.ir.validation import structural
from yambl_compiler.languages.yambl.computation.records import ValueField


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

    def test_multiple_unique_keys_have_order_independent_digest(self):
        source = """
@unique_by(self.a, self.b)
@unique_by(self.external)
entity A { id id: string attr a: string attr b: string attr external: string }
entity B inherits A {}
"""
        self.write('model.yambl', source)
        first = compile_repository(self.root)
        self.write(
            'model.yambl',
            source.replace(
                '@unique_by(self.a, self.b)\n@unique_by(self.external)',
                '@unique_by(self.external)\n@unique_by(self.a, self.b)',
            ),
        )
        second = compile_repository(self.root)
        self.assertEqual(first.model_digest, second.model_digest)
        self.assertEqual(validate_ir(loads(dumps(first))), ())
        self.assertEqual(validate_ir(loads(dumps(second))), ())

    def test_invalid_finite_ir_is_rejected_before_restoration(self):
        self.write('model.yambl', 'entity A { id id: string attr n: one_of[1,2] }')
        model = compile_repository(self.root)
        entity = next(item for item in model.definitions if item.identity == 'A')
        field = next(item for item in entity.fields if item.name == 'n')
        expression = field.expression
        self.assertIsNotNone(expression)
        values = expression.finite_values
        malformed = (
            (
                replace(field, expression=replace(expression, name='bad')),
                'invalid finite expression',
            ),
            (
                replace(
                    field, expression=replace(expression, finite_values=tuple(reversed(values)))
                ),
                'expected unique canonical ordering',
            ),
            (
                replace(
                    field,
                    expression=replace(expression, references=(Text('X', 'A'), Text('X', 'A'))),
                ),
                'duplicate finite expression binding',
            ),
            (
                replace(field, expression=replace(expression, kind='primitive')),
                'finite values require a finite expression',
            ),
            (
                replace(field, type=replace(field.type, allowed_values=tuple(reversed(values)))),
                'expected unique canonical ordering',
            ),
            (
                replace(
                    field,
                    type=replace(field.type, allowed_values=(Value('string', 'x', ''), values[1])),
                ),
                'member type disagrees',
            ),
        )
        for bad_field, expected in malformed:
            with self.subTest(expected=expected):
                bad_entity = replace(
                    entity, fields=tuple(bad_field if f == field else f for f in entity.fields)
                )
                damaged = replace(
                    model,
                    definitions=tuple(bad_entity if d == entity else d for d in model.definitions),
                )
                self.assertIn(expected, '\n'.join(structural(damaged)))

    def test_corrupted_finite_local_annotation_is_rejected(self):
        self.write(
            'model.yambl',
            'entity A { id id: string computed attr n: integer { let x: one_of[1,2] = 1 return x } }',
        )
        model = compile_repository(self.root)
        entity = next(item for item in model.definitions if item.identity == 'A')
        field = next(item for item in entity.fields if item.name == 'n')
        calculation = field.calculation
        binding = calculation.body.bindings[0]
        annotation = binding.annotation
        for damaged_hint, diagnostic in (
            (
                replace(annotation, finite_values=annotation.finite_values[:1]),
                'invalid finite annotation',
            ),
            (replace(annotation, name='integer'), 'finite values require a finite annotation'),
        ):
            with self.subTest(diagnostic=diagnostic):
                changed_binding = replace(binding, annotation=damaged_hint)
                changed_calculation = replace(
                    calculation, body=replace(calculation.body, bindings=(changed_binding,))
                )
                changed = replace(field, calculation=changed_calculation)
                bad_entity = replace(
                    entity, fields=tuple(changed if f == field else f for f in entity.fields)
                )
                damaged = replace(
                    model,
                    definitions=tuple(bad_entity if d == entity else d for d in model.definitions),
                )
                self.assertIn(diagnostic, '\n'.join(structural(damaged)))

    def test_invalid_portable_scalar_values_are_rejected(self):
        self.write('model.yambl', 'entity A { id id: string attr n: integer }')
        model = compile_repository(self.root)
        entity = next(item for item in model.definitions if item.identity == 'A')
        field = next(item for item in entity.fields if item.name == 'n')
        invalid = (
            Value('float', 'NaN', ''),
            Value('float', 'wrong', ''),
            Value('date', '2026-02-30', ''),
            Value('date', '20260101', ''),
            Value('datetime', '2026-01-01', ''),
            Value('datetime', '2026-01-01T00:00:00+00:00', ''),
            Value('integer', '1', '', fields=(ValueField('x', Value('integer', '2', '')),)),
            Value('structured', 'unexpected', '', fields=()),
        )
        for value in invalid:
            with self.subTest(value=value):
                changed = replace(field, type=replace(field.type, exact=value))
                bad_entity = replace(
                    entity, fields=tuple(changed if f == field else f for f in entity.fields)
                )
                damaged = replace(
                    model,
                    definitions=tuple(bad_entity if d == entity else d for d in model.definitions),
                )
                self.assertIn('invalid scalar value', '\n'.join(structural(damaged)))

    def test_finite_local_annotation_restores_constant_references(self):
        source = 'const LOW: integer = 2 entity A { id id: string computed attr number: integer { let x: one_of[LOW, 3] = 2 return x } }'
        self.write('model.yambl', source)
        model = compile_repository(self.root)
        self.write('model.yambl', source.replace('one_of[LOW, 3]', 'one_of[3, LOW]'))
        self.assertEqual(compile_repository(self.root).model_digest, model.model_digest)
        self.write('model.yambl', 'unavailable')
        self.assertEqual(validate_ir(loads(dumps(model))), ())

    def test_scalar_finite_values_have_canonical_ir(self):
        self.write(
            'model.yambl',
            """
const AMOUNT: decimal = 1.20
entity A {
    id id: string
    attr amount: one_of[AMOUNT, 2.30]
    attr day: one_of[parse_date('2026-01-01'), parse_date('2026-01-02')]
    attr ratio: one_of[1 / 2, 3 / 2]
}
""",
        )
        model = compile_repository(self.root)
        self.assertEqual(validate_ir(loads(dumps(model))), ())
        entity = next(item for item in model.definitions if item.identity == 'A')
        fields = {item.name: item for item in entity.fields}
        self.assertEqual({v.text for v in fields['amount'].type.allowed_values}, {'1.2', '2.3'})
        self.assertEqual(
            {v.text for v in fields['day'].type.allowed_values}, {'2026-01-01', '2026-01-02'}
        )
        self.assertEqual({v.text for v in fields['ratio'].type.allowed_values}, {'0.5', '1.5'})

    def test_nested_and_dictionary_local_annotations_round_trip(self):
        self.write(
            'model.yambl',
            """
const N: integer = 1
decodes D {}
@flags decodes F { - 1 A: 'a' }
entity X {
    id id: string
    computed attr value: Decodes { let x: exact<D> = D return x }
    computed attr choice: code<F> { let x: exact<F.A> = F.A return x }
    constraint nested { let xs: values<one_of[N, 2]> = [1] assert true }
}
""",
        )
        model = compile_repository(self.root)
        self.write('model.yambl', 'unavailable')
        self.assertEqual(validate_ir(loads(dumps(model))), ())

    def test_constraint_local_annotation_round_trip(self):
        self.write(
            'model.yambl',
            """
const LOW: integer = 2
entity A { id id: string constraint bounded { let x: one_of[LOW, 3] = 2 assert x > 0 } }
""",
        )
        model = compile_repository(self.root)
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
