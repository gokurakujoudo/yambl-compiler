"""Bundled model resources are valid and match the authoring repository."""

from pathlib import Path

from tests.support import FilesystemCase, document
from yambl_compiler.application.project import run
from yambl_compiler.semantics.builtins import AUTHOR_ROOT, DOCUMENTS, MODEL_ROOT, load_models
from yambl_compiler.semantics.symbols import namespace
from yambl_compiler.semantics.validation import check


class BuiltinTests(FilesystemCase):
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
            },
        )
        self.assertEqual((MODEL_ROOT / 'yambl-root.lclcfg').read_text(), "prefix: ''\n")
        if AUTHOR_ROOT.is_dir():
            self.assertTrue(run(AUTHOR_ROOT, 'check').ok)
            for file in MODEL_ROOT.rglob('*'):
                if not file.is_file():
                    continue
                self.assertEqual(
                    file.read_bytes(), (AUTHOR_ROOT / file.relative_to(MODEL_ROOT)).read_bytes()
                )

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

    def test_builtin_namespace_does_not_export_user_models_or_bypass_other_reserved_names(self):
        for text in [
            'entity Hierarchy {}',
            'namespace business entity Hierarchy {}',
            'namespace fmc.builtins entity Hierarchy {}',
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
