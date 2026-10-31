"""Compile rooted sources and independently consume their semantic artifacts."""

import contextlib
import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.languages.yambl.semantics.test_decorators import VALID as DECORATORS
from tests.languages.yambl.semantics.test_expressions import VALID as EXPRESSIONS
from tests.support import FilesystemCase
from yambl_compiler.application.cli import main, run_ir
from yambl_compiler.ir.application import compile_repository, load_ir, write_ir
from yambl_compiler.ir.codec import dumps, loads, seal
from yambl_compiler.ir.validation import validate_ir


class ArtifactTests(FilesystemCase):
    def repository(self, group, name='repo', prefix=''):
        root = self.root / name
        self.write(f'{name}/yambl-root.lclcfg', f"prefix: '{prefix}'\n")
        for i, text in enumerate([group] if isinstance(group, str) else group):
            self.write(f'{name}/file{i}.yambl', text)
        return root

    def test_compile_preserves_lookup_in_standalone_ir(self):
        root = self.repository(
            'entity E { id key: string lookup by_key { key = self.key::value() } }'
        )
        model = compile_repository(root)
        output = self.root / 'lookup.json'
        write_ir(model, output)
        (root / 'file0.yambl').write_text('source unavailable', encoding='utf-8')
        restored = load_ir(output)
        self.assertEqual(restored, model)
        entity = next(item for item in restored.definitions if item.identity == 'E')
        self.assertEqual([rule.name for rule in entity.lookups], ['by_key'])
        self.assertEqual(entity.lookups[0].keys[0].steps[0].access.identity, 'E.key')

    def test_string_format_references_round_trip(self):
        root = self.repository(
            [
                "namespace formats type fragment = string['A|B'] type other = string['C']",
                "namespace business from formats import fragment "
                "type combined = string['${fragment}_X'] "
                "type escaped = string['\\${fragment}'] entity E { id key: combined }",
            ]
        )
        model = compile_repository(root)
        restored = loads(dumps(model))
        self.assertEqual(validate_ir(restored), ())
        combined = next(item for item in restored.definitions if item.name == 'business.combined')
        self.assertEqual(combined.expression.parameters[0].value, '${fragment}_X')
        entity = next(item for item in restored.definitions if item.name == 'business.E')
        key = next(field for field in entity.fields if field.name == 'key')
        self.assertEqual(key.type.parameters[0].value, '(?:A|B)_X')
        escaped = next(item for item in restored.definitions if item.name == 'business.escaped')
        self.assertEqual(escaped.expression.references, ())
        expression = combined.expression
        for references, message in [
            ((), 'invalid string format bindings'),
            (
                (replace(expression.references[0], value='formats.missing'),),
                'missing string format',
            ),
            (
                (replace(expression.references[0], value='formats.other'),),
                'invalid format reference',
            ),
        ]:
            changed = replace(combined, expression=replace(expression, references=references))
            damaged = seal(
                replace(
                    restored,
                    definitions=tuple(
                        changed if d == combined else d for d in restored.definitions
                    ),
                )
            )
            self.assertIn(message, '\n'.join(validate_ir(damaged)))

    def test_lookup_multiple_dimensions_survive_annotation_reordering(self):
        source = '''entity E {
    id key: string
    dimension provider { value provider: string }
    dimension agency { value agency: string }
    @dimension(provider) @dimension(agency) @temporal(day: date) scd attr x: string
    lookup by_x { x = self.x::value() }
}
'''
        root = self.repository(source)
        first = compile_repository(root)
        entity = next(item for item in first.definitions if item.identity == 'E')
        step = entity.lookups[0].keys[0].steps[0]
        self.assertEqual([item.name for item in step.bindings], ['agency', 'day', 'provider'])
        (root / 'file0.yambl').write_text(
            source.replace(
                '@dimension(provider) @dimension(agency)', '@dimension(agency) @dimension(provider)'
            ),
            encoding='utf-8',
        )
        second = compile_repository(root)
        self.assertEqual(first.model_digest, second.model_digest)
        self.assertEqual(validate_ir(loads(dumps(second))), ())

    def test_lookup_string_dimension_alias_and_reference_rejection(self):
        source = '''type text_alias = string
const SOURCE: string = 'feed'
entity E {
    id key: string
    dimension d { value source: text_alias }
    @dimension(d) attr x: string
    lookup by_literal { x = self.x::value(source='feed') }
    lookup by_constant { x = self.x::value(source=SOURCE) }
}
'''
        model = compile_repository(self.repository(source, 'valid'))
        entity = next(item for item in model.definitions if item.identity == 'E')
        for rule in entity.lookups:
            binding = rule.keys[0].steps[0].bindings[0]
            self.assertEqual((binding.fixed_kind, binding.fixed_text), ('string', 'feed'))
        self.assertEqual(validate_ir(loads(dumps(model))), ())

        for dimension_type in ('Issuer', 'issuer_alias', 'integer_alias'):
            with self.subTest(dimension_type=dimension_type):
                bad = '''entity Issuer { id id: integer }
type issuer_alias = Issuer
type integer_alias = integer
entity E { id key: string dimension d { value source: TYPE } }
'''.replace('TYPE', dimension_type)
                root = self.repository(bad, 'invalid_' + dimension_type)
                with self.assertRaisesRegex(ValueError, 'dimension value must be string or code'):
                    compile_repository(root)

    def test_full_expression_and_metadata_roundtrips(self):
        for i, sources in enumerate([EXPRESSIONS, *DECORATORS]):
            with self.subTest(case=i):
                model = compile_repository(self.repository(sources, f'case{i}'))
                self.assertEqual(validate_ir(model), ())
                self.assertEqual(loads(dumps(model)), model)
                self.assertEqual(dumps(loads(dumps(model))), dumps(model))
        definitions = {
            d.name: d for d in compile_repository(self.repository(EXPRESSIONS)).definitions
        }
        selection = definitions['core.Selection']
        fields = {f.name: f for f in selection.fields}
        self.assertEqual(fields['c1'].expression.accesses[0].target, 'core.Selection.kind')
        self.assertTrue(fields['c1'].type.dependent)
        self.assertEqual(fields['mapping'].type.name, 'mapping')
        self.assertEqual(fields['shortcut'].expression.target, 'builtins.field_mapping')
        self.assertEqual(fields['shortcut'].expanded.name, 'mapping')
        self.assertEqual(fields['c5'].expanded.kind, 'self')
        self.assertEqual(fields['c5'].expanded.accesses[0].target, 'core.Selection.kind')
        self.assertIn('core.Security.currency', fields['fields'].type.candidates)
        self.assertEqual(definitions['core.Bond'].fields[0].identity, 'core.Security.currency')
        logical = {f.identity: f for f in compile_repository(self.root / 'repo').fields}
        self.assertEqual(
            logical['core.Security.currency'].implementations,
            ('core.Bond.currency', 'core.Security.currency'),
        )
        self.assertEqual(
            logical['core.Security.currency'].applicable, ('core.Bond', 'core.Security')
        )
        self.assertIsNone(logical['core.Security.currency'].type.exact)

    def test_relocated_sources_and_comment_changes_preserve_semantics(self):
        text = "namespace core entity A { id key: string config attr label: string = 'quote\\\'s' }"
        first = compile_repository(self.repository(text, 'first', 'demo'))
        second = compile_repository(self.repository(text, 'second', 'demo'))
        self.assertEqual(dumps(first), dumps(second))
        file = self.root / 'second/file0.yambl'
        file.write_text('# private comment\n---\nprivate Markdown\n---\n' + text, encoding='utf-8')
        changed = compile_repository(file)
        self.assertEqual(first.model_digest, changed.model_digest)
        self.assertNotEqual(dumps(first), dumps(changed))
        self.assertNotIn('private', dumps(changed))
        self.assertNotIn(str(self.root), dumps(first))
        self.assertEqual(first, compile_repository(self.root / 'first/file0.yambl'))

    def test_standalone_process_never_loads_builtin_resources(self):
        model = compile_repository(self.repository("entity A { attr x: code<'Prices'> }"))
        output = self.root / 'artifact.json'
        write_ir(model, output)
        script = '''
import sys
from pathlib import Path
original = Path.read_text
def guarded(path, *args, **kwargs):
    if path.suffix == '.yambl' or path.name == 'result-constants.json':
        raise AssertionError('unexpected source access: ' + str(path))
    return original(path, *args, **kwargs)
Path.read_text = guarded
from yambl_compiler.ir.application import load_ir
model = load_ir(Path(sys.argv[1]))
assert 'yambl_compiler.languages.yambl.semantics.builtins' not in sys.modules
print(model.model_digest)
'''
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', script, str(output)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), model.model_digest)
        self.assertEqual(model.external_dictionaries[0].name, 'Prices')

    def test_cli_compile_check_and_dry_run(self):
        root = self.repository("entity A { attr code: code<'External'> }")
        output = self.root / 'build/model.yambl-ir.json'
        commands = [
            (['compile', '-o', 'path', str(root)], 1, 'output'),
            (
                ['compile', '-o', 'path', str(root), '-o', 'output', str(output), '--dryrun'],
                0,
                'would write',
            ),
            (['compile', '-o', 'path', str(root), '-o', 'output', str(output)], 0, 'compiled'),
            (['check-ir', '-o', 'path', str(output)], 0, 'checked IR'),
            (
                [
                    'compile',
                    '-o',
                    'path',
                    str(root),
                    '-o',
                    'output',
                    str(output.with_suffix('.yml')),
                ],
                1,
                '.json',
            ),
            (['check-ir', '-o', 'path', str(output.with_name('absent.json'))], 1, 'absent.json'),
        ]
        for args, status, message in commands:
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(main(['python', 'yambl.py', *args]), status)
            self.assertIn(message, captured.getvalue())
            if '--dryrun' in args:
                self.assertFalse(output.parent.exists())
        self.assertEqual(load_ir(output), compile_repository(root))
        self.assertFalse(run_ir(root, 'compile', output, True).messages == [])
        (root / 'file0.yambl').write_text('bad syntax', encoding='utf-8')
        before = output.read_bytes()
        self.assertFalse(run_ir(root, 'compile', output, False).ok)
        self.assertEqual(output.read_bytes(), before)

    def test_discovery_errors_and_atomic_publication(self):
        with self.assertRaisesRegex(ValueError, 'requires a repository'):
            compile_repository(self.root)
        root = self.repository('entity A {}')
        model = compile_repository(root)
        output = self.root / 'artifact.json'
        output.write_text('old artifact', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '.json'):
            write_ir(model, output.with_suffix('.yaml'))
        with self.assertRaisesRegex(ValueError, 'revision'):
            write_ir(replace(model, format_revision=99), output)
        for operation in ['write', 'replace']:
            with self.subTest(operation=operation):
                target = (
                    'yambl_compiler.ir.application.os.replace'
                    if operation == 'replace'
                    else 'pathlib.Path.write_text'
                )
                with patch(target, side_effect=OSError('publication denied')):
                    with self.assertRaisesRegex(OSError, 'publication denied'):
                        write_ir(model, output)
                self.assertEqual(output.read_text(), 'old artifact')
                self.assertEqual(list(self.root.glob('.yambl-ir-*')), [])
        write_ir(model, output)
        self.assertEqual(load_ir(output), model)
        output.write_text(dumps(replace(model, model_digest='bad')), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            load_ir(output)
        (root / 'file0.yambl').write_text('entity A { attr missing: Missing }', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unknown type'):
            compile_repository(root)

    def test_all_valid_editor_corpus_compiles(self):
        from tests.support import document
        from yambl_compiler.languages.yambl.semantics.validation import check

        fixtures = Path(__file__).resolve().parents[1] / 'fixtures/editor'
        count = 0
        for filename in [
            'cases.json',
            'expression-cases.json',
            'flags-cases.json',
            'decorator-cases.json',
        ]:
            for group in json.loads((fixtures / filename).read_text(encoding='utf-8')):
                try:
                    docs = [document(text, f'{i}.yambl') for i, text in enumerate(group)]
                except AssertionError:
                    continue
                if any(d.severity == 'error' for d in check(docs)):
                    continue
                with self.subTest(group=group):
                    model = compile_repository(self.repository(group, f'corpus{count}'))
                    self.assertEqual(validate_ir(model), ())
                count += 1
        self.assertGreater(count, 50)

    def test_dynamic_flags_captured_defaults_and_quoted_local_dictionary(self):
        model = compile_repository(self.repository("""
@targets(DecoratorTargets.Entity)
decorator @linked(target: type<self.child::type>)
@targets(DecoratorTargets.Entity)
decorator @dictionary(value: Decodes)
entity Child {}
decodes Codes { - 1 First: 'First' }
@linked(Child)
@dictionary('external-id')
entity A {
    attr child: Child
    attr categories: code<AttrCats>
    attr selected: attrs<AttrCats.Static + self.categories::value, A>
    attr local: code<'Codes'>
}
"""))
        declaration = next(d for d in model.definitions if d.name == 'A')
        value = (
            next(c for c in declaration.decorators if c.target == 'dictionary').bound[0].type.exact
        )
        self.assertEqual(
            (value.kind, value.text, value.target), ('reference', 'external-id', 'builtins.Decodes')
        )
        self.assertEqual(model.external_dictionaries, ())
        selected = next(f for f in declaration.fields if f.name == 'selected')
        self.assertEqual(selected.expanded.arguments[0].operations[0].operand.kind, 'self')
        local = compile_repository(
            self.repository("namespace builtins entity A { attr x: code<'External'> }", 'local')
        )
        self.assertEqual(local.external_dictionaries[0].name, 'model::builtins.External')

    def test_builtin_root_constraints_and_failed_ir_validation(self):
        root = self.repository('namespace builtins entity A {}', prefix='wrong')
        with patch('yambl_compiler.languages.yambl.semantics.builtins.AUTHOR_ROOT', root):
            with self.assertRaisesRegex(ValueError, 'built-in prefix must be empty'):
                compile_repository(root)
            (root / 'yambl-root.lclcfg').write_text("prefix: ''\n", encoding='utf-8')
            (root / 'file0.yambl').write_text('namespace other entity A {}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'must declare namespace builtins'):
                compile_repository(root)
        (root / 'file0.yambl').write_text('entity A {}', encoding='utf-8')
        with patch(
            'yambl_compiler.ir.application.validate_ir', return_value=('inconsistent view',)
        ):
            with self.assertRaisesRegex(ValueError, 'inconsistent view'):
                compile_repository(root)
        from yambl_compiler.languages.yambl.semantics.builtins import MODEL_ROOT

        model = compile_repository(MODEL_ROOT)
        self.assertTrue(all(d.source.origin == 'builtin' for d in model.definitions))
