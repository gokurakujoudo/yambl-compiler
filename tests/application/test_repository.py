"""Repository discovery, literal prefixes and isolation."""

from dataclasses import replace
from unittest.mock import patch

from tests.support import FilesystemCase, document
from yambl_compiler.application.project import run, source_files
from yambl_compiler.application.repository import find_root, read_prefix
from yambl_compiler.languages.yambl.semantics.symbols import namespace, resolve_scopes
from yambl_compiler.languages.yambl.semantics.validation import check


class RepositoryTests(FilesystemCase):
    def test_literal_prefix_and_rejections(self):
        path = self.write('yambl-root.lclcfg', "# comment\n\nprefix: 'fmc.data' # comment\n")
        self.assertEqual(read_prefix(path), 'fmc.data')
        for content in ['prefix: ""', "prefix: 'fmc'", 'prefix: "fmc.data"']:
            path.write_text(content, encoding='utf-8')
            self.assertIsInstance(read_prefix(path), str)
        for content in [
            '',
            '# comment',
            'prefix: 3',
            "other: ''",
            "prefix: ''\nprefix: ''",
            "prefix: 'bad..name'",
            "prefix: 'fmc' + '.core'",
        ]:
            with self.subTest(content=content):
                path.write_text(content, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'prefix'):
                    read_prefix(path)

    def test_whole_repository_and_relative_import(self):
        root = self.write('yambl-root.lclcfg', "prefix: 'fmc'").parent
        source = self.write(
            'core/item.yambl',
            'namespace core from dict import Codes entity Item { attr x: code<Codes> }',
        )
        self.write('dict/codes.yambl', 'namespace dict decodes Codes {}')
        self.assertEqual(find_root(source), root)
        self.assertTrue(run(source.parent, 'check').ok)
        self.assertIn('checked 2 file(s)', run(source, 'check').messages)
        source.write_text(
            'namespace core from fmc.dict import Codes entity Item {}', encoding='utf-8'
        )
        self.assertFalse(run(source, 'check').ok)
        source.write_text('entity Item{}', encoding='utf-8')
        self.assertTrue(run(source, 'format').ok)
        self.assertEqual((root / 'dict/codes.yambl').read_text(), 'namespace dict decodes Codes {}')
        self.assertFalse(run(root / 'missing.yambl', 'check').ok)

    def test_nested_and_multiple_roots(self):
        self.write('yambl-root.lclcfg', "prefix: ''")
        self.write('a.yambl', 'entity A {}')
        self.write('child/yambl-root.lclcfg', "prefix: ''")
        self.write('child/b.yambl', 'entity B {}')
        self.assertIn('nested or multiple', ' '.join(run(self.root, 'check').messages))
        (self.root / 'yambl-root.lclcfg').unlink()
        self.assertIn('nested or multiple', ' '.join(run(self.root, 'check').messages))

    def test_standalone_and_qualified_identity(self):
        self.assertIsNone(find_root(self.root))
        path = self.write('a.yambl', 'entity A { attr h: Hierarchy }')
        self.assertTrue(run(path, 'check').ok)
        first = replace(document('entity A {}'), prefix='fmc', repository='one')
        second = replace(first, repository='two')
        self.assertEqual(namespace(first), 'fmc')
        self.assertEqual(check([first, second]), [])
        units = resolve_scopes([first, second], [])
        self.assertNotEqual(units[0].scope['A'].key, units[1].scope['A'].key)

    def test_non_regular_files_are_rejected(self):
        path = self.write('pipe.yambl', '')
        with (
            patch('pathlib.Path.is_file', return_value=False),
            patch('pathlib.Path.is_dir', return_value=False),
        ):
            with self.assertRaisesRegex(ValueError, 'directory or file does not exist'):
                source_files(path, False)

    def test_builtin_repository_constraints(self):
        self.write('yambl-root.lclcfg', "prefix: 'wrong'")
        source = self.write('model.yambl', 'entity A {}')
        with patch('yambl_compiler.languages.yambl.semantics.builtins.AUTHOR_ROOT', self.root):
            self.assertIn(
                'built-in prefix must be empty', ' '.join(run(self.root, 'check').messages)
            )
            (self.root / 'yambl-root.lclcfg').write_text("prefix: ''", encoding='utf-8')
            source.write_text('namespace bad entity A {}', encoding='utf-8')
            self.assertIn(
                'must declare namespace builtins', ' '.join(run(self.root, 'check').messages)
            )
