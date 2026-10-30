"""Bundled model resources are valid and match the authoring repository."""

from pathlib import Path

from tests.support import FilesystemCase, document, load_generated
from yambl_compiler.application.project import run
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.yambl.semantics.builtins import (
    AUTHOR_ROOT,
    DOCUMENTS,
    MODEL_ROOT,
    RESULT_CONSTANTS,
    load_models,
)
from yambl_compiler.languages.yambl.semantics.symbols import namespace
from yambl_compiler.languages.yambl.semantics.validation import check
from yambl_compiler.storage.generation import generate


class BuiltinTests(FilesystemCase):
    def test_result_constants_have_distinct_meanings_and_reserved_names(self):
        expected = {
            'NOT_DEFINED': 'actual type',
            'NOT_APPLICABLE': 'business context',
            'EMPTY': 'record exists',
            'NO_ENTRY': 'does not exist in storage',
            'NO_DATA': 'retrieval requirements',
            'ENTITY_NOT_FOUND': 'Dereferencing',
            'ERROR': 'error occurred',
            "PARTIAL": "usable data",
        }
        self.assertEqual(set(RESULT_CONSTANTS), set(expected))
        for name, meaning in expected.items():
            with self.subTest(name=name):
                self.assertIn(meaning, RESULT_CONSTANTS[name])
                for prefix in ['', 'namespace builtins ', 'namespace business ']:
                    messages = [d.message for d in check([document(f"{prefix}entity {name} {{}}")])]
                    self.assertIn(f"duplicate or reserved declaration: {name}", messages)

    def test_result_constants_are_not_types_or_assignable_literals(self):
        for name in RESULT_CONSTANTS:
            for source in [
                f"entity A {{ attr x: {name} }}",
                f"type label = {name}",
                f"@title({name}) entity A {{}}",
                f"entity A {{ attr x: exact<{name}> }}",
                f"entity A {{ attr x: {name} + AttrCats.Static }}",
                f"@targets(DecoratorTargets.All) decorator @note(text: string = {name})",
            ]:
                with self.subTest(source=source):
                    messages = [d.message for d in check([document(source)])]
                    self.assertIn(
                        f"result constant {name} cannot be used in model expressions",
                        messages,
                    )
                    self.assertNotIn(f"unknown type: {name}", messages)
            self.assertEqual(
                check(
                    [
                        document(
                            f"@title('{name}') entity A {{ config attr text: string = '{name}' }}"
                        )
                    ]
                ),
                [],
            )
        self.assertEqual(
            check(
                [document("decodes Codes { - 1 ERROR: 'Error' } @title(Codes.ERROR) entity A {}")]
            ),
            [],
        )
        self.assertEqual(check([document('entity Empty {} entity A { attr x: Empty }')]), [])

    def test_models_validate_without_self_redeclaration(self):
        self.assertEqual(check(DOCUMENTS), [])
        self.assertEqual({namespace(doc) for doc in DOCUMENTS}, {'builtins'})
        self.assertTrue(run(MODEL_ROOT, 'check').ok)
        self.assertEqual(
            {p.name for p in MODEL_ROOT.rglob('*.yambl')},
            {
                'config_group.yambl',
                'decodes.yambl',
                'hierarchy.yambl',
                'attr_cats.yambl',
                'field_mapping.yambl',
                'decorators.yambl',
                'decorator_targets.yambl',
                'time_zones.yambl',
            },
        )
        self.assertEqual((MODEL_ROOT / 'yambl-root.lclcfg').read_text(), "prefix: ''\n")
        if AUTHOR_ROOT.is_dir():
            self.assertTrue(run(AUTHOR_ROOT, 'check').ok)
            for file in MODEL_ROOT.rglob('*'):
                if not file.is_file():
                    continue
                self.assertEqual(
                    file.read_bytes(),
                    (AUTHOR_ROOT / file.relative_to(MODEL_ROOT)).read_bytes(),
                )

    def test_time_zones_are_visible_and_fixed(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl', 'entity Event { id id: string static attr zone: code<TimeZones> }'
        )
        model = compile_repository(self.root)
        zones = next(item for item in model.definitions if item.identity == 'builtins.TimeZones')
        self.assertEqual(len(zones.entries), 39)
        codes = {entry.code: int(entry.ordering) for entry in zones.entries}
        self.assertEqual(len(codes), 39)
        self.assertEqual(len(set(codes.values())), 39)
        self.assertEqual(
            {
                name: codes[name]
                for name in ('UTC', 'AsiaShanghai', 'AsiaHongKong', 'AsiaSingapore')
            },
            {'UTC': 0, 'AsiaShanghai': 801, 'AsiaHongKong': 803, 'AsiaSingapore': 804},
        )
        self.assertNotIn('AsiaBeijing', codes)
        output = self.root / 'generated'
        layout = generate(model, output, 'sample_model')
        package = load_generated(output / 'sample_model', 'sample_model')
        self.assertEqual(package.TimeZonesCode.AsiaShanghai.value, 'AsiaShanghai')
        table = next(table for table in layout.tables if table.name == 'tb_decodes_entries')
        rows = [row for row in table.rows if row[0] == 'builtins.TimeZones']
        self.assertEqual(len(rows), 39)
        self.assertIn(('builtins.TimeZones', 'AsiaShanghai', 'Asia/Shanghai', '801'), rows)

    def test_invalid_resource_is_not_silently_accepted(self):
        file = self.write('a.yambl', 'bad syntax')
        with self.assertRaisesRegex(ValueError, 'invalid YAMBL syntax'):
            load_models(self.root)
        file.write_text('namespace bad entity A {}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'must declare namespace builtins'):
            load_models(self.root)
        file.write_text('value a {}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'must declare namespace builtins'):
            load_models(self.root)
        file.write_text('namespace builtins value a {}', encoding='utf-8')
        self.assertEqual(load_models(self.root)[0].path, Path(file))

    def test_authoring_models_in_an_independent_checkout(self):
        for file in MODEL_ROOT.rglob('*'):
            if file.is_file():
                self.write(str(file.relative_to(MODEL_ROOT)), file.read_text(encoding='utf-8'))
        self.assertTrue(run(self.root, 'check').ok)
        self.assertEqual(check([document(doc.text) for doc in DOCUMENTS]), [])
        self.write('duplicate.yambl', 'namespace builtins final entity Hierarchy {}')
        self.assertIn(
            'duplicate or reserved declaration: Hierarchy',
            ' '.join(run(self.root, 'check').messages),
        )

    def test_builtin_namespace_does_not_export_user_models_or_bypass_other_reserved_names(
        self,
    ):
        for text in [
            'entity Hierarchy {}',
            'namespace business entity Hierarchy {}',
            'namespace demo.builtins entity Hierarchy {}',
            'namespace builtins entity Entity {}',
            'namespace builtins value string {}',
        ]:
            with self.subTest(text=text):
                self.assertTrue(
                    any('duplicate or reserved' in d.message for d in check([document(text)]))
                )
        self.assertEqual(
            [
                d.message
                for d in check(
                    [
                        document('namespace builtins entity Private {}'),
                        document(
                            'namespace business entity A { attr x: Private attr h: Hierarchy }'
                        ),
                    ]
                )
            ],
            ['unknown type: Private'],
        )
