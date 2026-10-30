"""Check decorator variadic bindings in a standalone intermediate model."""

from dataclasses import replace

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository, validate_ir
from yambl_compiler.ir.codec import dumps, loads, seal


class VariadicIRTests(FilesystemCase):
    def test_unique_keys_store_resolved_fields_and_sources(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        source = (
            "@unique_by(self.x, self.y) entity A { id id: string attr x: string attr y: integer }"
        )
        self.write('model.yambl', source)
        model = compile_repository(self.root)
        entity = next(item for item in model.definitions if item.identity == 'A')
        self.assertEqual(entity.unique_keys[0].fields, ('A.x', 'A.y'))
        self.assertEqual(entity.unique_keys[0].entity, 'A')
        self.assertEqual(
            [item.type.exact.text for item in entity.decorators[0].bound], ['A.x', 'A.y']
        )
        self.assertEqual(validate_ir(loads(dumps(model))), ())
        invalid = replace(entity.unique_keys[0], fields=('A.x', 'A.x'))
        damaged = seal(
            replace(
                model,
                definitions=tuple(
                    replace(item, unique_keys=(invalid,)) if item == entity else item
                    for item in model.definitions
                ),
            )
        )
        self.assertIn('invalid unique key', '\n'.join(validate_ir(damaged)))

    def test_variadic_arguments_round_trip_and_invalid_formal_is_rejected(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            "@targets(DecoratorTargets.Entity) decorator @labels(prefix: integer, ...values: string) "
            "@labels(1, 'a', 'b', description='note') entity A { id id: string }",
        )
        model = compile_repository(self.root)
        declaration = next(item for item in model.definitions if item.name == 'labels')
        self.assertTrue(declaration.parameters[-1].variadic)
        self.assertEqual(validate_ir(loads(dumps(model))), ())
        invalid = replace(declaration.parameters[-1], optional=True)
        damaged = seal(
            replace(
                model,
                definitions=tuple(
                    (
                        replace(item, parameters=(*item.parameters[:-1], invalid))
                        if item == declaration
                        else item
                    )
                    for item in model.definitions
                ),
            )
        )
        self.assertIn('invalid variadic parameter', '\n'.join(validate_ir(damaged)))
