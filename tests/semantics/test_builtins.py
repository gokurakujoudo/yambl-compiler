"""Bundled model resources are valid and match the authoring repository."""

from pathlib import Path

from tests.support import FilesystemCase
from yambl_compiler.application.project import run
from yambl_compiler.semantics.builtins import AUTHOR_ROOT, DOCUMENTS, MODEL_ROOT, load_models
from yambl_compiler.semantics.validation import check


class BuiltinTests(FilesystemCase):
    def test_models_validate_without_self_redeclaration(self):
        self.assertEqual(check(DOCUMENTS), [])
        self.assertTrue(run(MODEL_ROOT, 'check').ok)
        self.assertEqual(
            {p.name for p in MODEL_ROOT.glob('*.yambl')},
            {'config_group.yambl', 'decodes.yambl', 'hierarchy.yambl'},
        )
        self.assertEqual((MODEL_ROOT / 'yambl-root.lclcfg').read_text(), "prefix: ''\n")
        if AUTHOR_ROOT.is_dir():
            self.assertTrue(run(AUTHOR_ROOT, 'check').ok)
            for file in MODEL_ROOT.iterdir():
                self.assertEqual(file.read_bytes(), (AUTHOR_ROOT / file.name).read_bytes())

    def test_invalid_resource_is_not_silently_accepted(self):
        file = self.write('a.yambl', 'bad syntax')
        with self.assertRaisesRegex(ValueError, 'invalid YAMBL syntax'):
            load_models(self.root)
        file.write_text('namespace bad entity A {}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'cannot declare a namespace'):
            load_models(self.root)
        file.write_text('value a {}', encoding='utf-8')
        self.assertEqual(load_models(self.root)[0].path, Path(file))
