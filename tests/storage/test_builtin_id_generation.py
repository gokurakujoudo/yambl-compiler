"""Reject unsupported built-in IDs in independently validated IR."""

from dataclasses import replace
from pathlib import Path

from tests.support import FilesystemCase, document
from yambl_compiler.ir import load_ir
from yambl_compiler.ir.codec import dumps
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.languages.yambl.semantics.builtins import DOCUMENTS, RESULT_CONSTANTS
from yambl_compiler.languages.yambl.semantics.symbols import resolve_scopes
from yambl_compiler.languages.yambl.semantics.validation import analyze_units
from yambl_compiler.storage.generation import generate


class BuiltinIdGenerationTests(FilesystemCase):
    def test_unsupported_builtin_ids_are_rejected_before_publication(self):
        for filename, entity, field in (
            ('decodes.yambl', 'Decodes', 'decodes_id'),
            ('hierarchy.yambl', 'Hierarchy', 'hierarchy_id'),
            ('config_group.yambl', 'ConfigGroup', 'config_group_id'),
        ):
            original = next(item for item in DOCUMENTS if item.path.name == filename)
            declaration = f'id {field}: string'
            self.assertIn(declaration, original.text)
            variants = (
                ('code', f"id {field}: code<'External'>"),
                ('missing', ''),
                ('multiple', declaration + '\n    id extra_key: string'),
                ('renamed', 'id different_key: string'),
            )
            for variant, replacement in variants:
                with self.subTest(entity=entity, variant=variant):
                    text = original.text.replace(declaration, replacement, 1)
                    self.check_rejected_model(original, text, entity, field, variant)

    def test_inherited_builtin_id_is_rejected_before_publication(self):
        original = next(item for item in DOCUMENTS if item.path.name == 'hierarchy.yambl')
        text = original.text.replace(
            'final entity Hierarchy {',
            'entity HierarchyBase { id inherited_key: string }\n'
            'final entity Hierarchy inherits HierarchyBase {',
            1,
        ).replace('    id hierarchy_id: string\n', '', 1)
        self.assertNotEqual(text, original.text)
        self.check_rejected_model(original, text, 'Hierarchy', 'hierarchy_id', 'inherited')

    def check_rejected_model(self, original, text, entity, field, variant):
        changed = replace(document(text, str(original.path)), builtin=True)
        builtins = [changed if item.path == original.path else item for item in DOCUMENTS]
        diagnostics = []
        units = resolve_scopes(
            [document('entity E { id id: string }')],
            diagnostics,
            builtins,
            RESULT_CONSTANTS,
        )
        analysis = analyze_units(units, diagnostics)
        self.assertEqual(analysis.diagnostics, ())
        model = Lowering(analysis, Path('.'), '', 'test').model()
        source = self.root / f'{entity}_{variant}.json'
        source.write_text(dumps(model), encoding='utf-8')
        checked = load_ir(source)
        output = self.root / f'generated_{entity}_{variant}'

        with self.assertRaises(ValueError) as caught:
            generate(checked, output, 'sample_model')

        message = str(caught.exception)
        self.assertIn(f'builtins.{entity}', message)
        self.assertIn(f'directly declared string ID {field}', message)
        self.assertFalse(output.exists())
