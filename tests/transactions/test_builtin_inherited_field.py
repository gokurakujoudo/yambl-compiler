"""Report unsupported inherited built-in fields through the generated Plan API."""

import importlib
import sys
from dataclasses import replace
from pathlib import Path

from tests.support import FilesystemCase, document
from yambl_compiler.ir import load_ir
from yambl_compiler.ir.codec import dumps
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.yambl.semantics.builtins import DOCUMENTS, RESULT_CONSTANTS
from yambl_compiler.languages.yambl.semantics.symbols import resolve_scopes
from yambl_compiler.languages.yambl.semantics.validation import analyze_units
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.validation import PlanValidator


class BuiltinInheritedFieldTests(FilesystemCase):
    def test_plan_rejects_inherited_field_without_storage(self):
        original = next(item for item in DOCUMENTS if item.path.name == 'hierarchy.yambl')
        source = original.text + '\nentity BuiltinBase { id base_id: string attr text: string }\n'
        changed = replace(document(source, str(original.path)), builtin=True)
        builtins = [changed if item.path == original.path else item for item in DOCUMENTS]
        diagnostics = []
        units = resolve_scopes(
            [document('entity E inherits BuiltinBase {}')], diagnostics, builtins, RESULT_CONSTANTS
        )
        analysis = analyze_units(units, diagnostics)
        self.assertEqual(analysis.diagnostics, ())
        model = Lowering(analysis, Path('.'), '', 'test').model()
        ir = self.root / 'model.json'
        ir.write_text(dumps(model), encoding='utf-8')
        checked = load_ir(ir)
        output = self.root / 'generated'
        layout = generate(checked, output, 'builtin_inheritance_model')
        self.assertNotIn('builtins.BuiltinBase.text', layout.fields)
        plan = self.root / 'plan.yaplan'
        plan.write_text(
            "plan p {\n"
            "    version 1\n"
            "    group g {\n"
            "        insert E (base_id, text) {\n"
            "            'one', 'hello'\n"
            "        }\n"
            "    }\n"
            "}\n",
            encoding='utf-8',
        )
        builtin_plan = self.root / 'builtin-plan.yaplan'
        builtin_plan.write_text(
            "plan p {\n"
            "    version 1\n"
            "    group g {\n"
            "        insert builtins.BuiltinBase (base_id) {\n"
            "            'one'\n"
            "        }\n"
            "    }\n"
            "}\n",
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, r'entity has no storage: builtins\.BuiltinBase'):
            PlanValidator(layout).validate(parse(builtin_plan.read_text(encoding='utf-8')))

        sys.path.insert(0, str(output))
        try:
            package = importlib.import_module('builtin_inheritance_model')
            with self.assertRaisesRegex(
                ValueError, r'field has no storage: builtins\.BuiltinBase\.text'
            ):
                package.BuiltinInheritanceData.validate_plan_syntax(plan)
            with self.assertRaisesRegex(
                ValueError, r'entity has no storage: builtins\.BuiltinBase'
            ):
                package.BuiltinInheritanceData.validate_plan_syntax(builtin_plan)
        finally:
            sys.path.remove(str(output))
            for name in tuple(sys.modules):
                if name == 'builtin_inheritance_model' or name.startswith(
                    'builtin_inheritance_model.'
                ):
                    del sys.modules[name]
