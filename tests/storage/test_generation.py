"""Verify generated schema, documentation, imports and publication failures."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.storage.test_runtime import SOURCE
from tests.support import FilesystemCase
from yambl_compiler.application.cli import Arguments, run_generation
from yambl_compiler.ir import compile_repository, write_ir
from yambl_compiler.storage.generation import generate, python_package
from yambl_compiler.storage.layout import build_layout, quote, supported, type_label
from yambl_compiler.storage.rendering import markdown_cell


class GenerationTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', SOURCE)
        self.model = compile_repository(self.root)

    def test_all_documented_tables_match_sqlite_and_manifest(self):
        output = self.root / 'output'
        layout = generate(self.model, output, 'sample_model')
        schema = (output / 'schema.sql').read_text()
        reference = (output / 'schema.md').read_text()
        manifest = json.loads((output / 'storage-manifest.json').read_text())
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript(schema)
            self.assertEqual(
                {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                {t.name for t in layout.tables},
            )
            for table in layout.tables:
                self.assertIn('## ' + table.name, reference)
                columns = list(db.execute('PRAGMA table_info(' + quote(table.name) + ')'))
                self.assertEqual(
                    [(r[1], r[2], not bool(r[3]), r[4]) for r in columns],
                    [(c.name, c.sql_type, c.nullable, c.default or None) for c in table.columns],
                )
                self.assertEqual(
                    tuple(r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]), table.primary
                )
                self.assertEqual(
                    len(list(db.execute('PRAGMA foreign_key_list(' + quote(table.name) + ')'))),
                    len(table.primary) - 1 if table.foreign_keys else 0,
                )
                for column in table.columns:
                    self.assertIn('| ' + column.name + ' |', reference)
                for check in table.checks:
                    self.assertIn('CHECK (' + check + ')', reference)
                for i, _ in enumerate(table.indexes):
                    self.assertIn(table.name + '__index_' + str(i), reference)
            self.assertEqual(
                dict(db.execute('SELECT * FROM yambl_metadata')),
                {'layout_revision': '1', 'model_digest': self.model.model_digest},
            )
        self.assertEqual(len(manifest['tables']), len(layout.tables))
        self.assertIn('model default: 20', reference)
        self.assertIn('not SQL defaults', reference)
        self.assertIn('not by a target foreign key', reference)
        self.assertNotEqual(layout.fields['A.limit'].table, layout.fields['A.text'].table)
        self.assertNotEqual(layout.fields['A.price'].table, layout.fields['A.positions'].table)
        self.assertEqual(layout.fields['A.text'].table, layout.fields['A.extra'].table)
        self.assertEqual(markdown_cell('中文|`<>&\r\n'), '中文&#124;&#96;&lt;&gt;&amp;<br>')
        with self.assertRaisesRegex(ValueError, 'new or empty'):
            generate(self.model, output, 'sample_model')
        self.assertEqual((output / 'schema.sql').read_text(), schema)
        other = self.root / 'other'
        other.mkdir()
        generate(self.model, other, 'sample_model')
        self.assertEqual((other / 'schema.md').read_bytes(), (output / 'schema.md').read_bytes())
        self.assertEqual(
            (other / 'sample_model/__init__.py').read_bytes(),
            (output / 'sample_model/__init__.py').read_bytes(),
        )

    def test_dry_run_and_failed_writes_preserve_destination(self):
        output = self.root / 'missing' / 'generated'
        generate(self.model, output, 'sample_model', True)
        self.assertFalse(output.parent.exists())
        for package in ('../bad', 'class', ''):
            with self.assertRaisesRegex(ValueError, 'package'):
                generate(self.model, output, package)
        blocked = self.write('file', 'keep')
        with self.assertRaisesRegex(ValueError, 'new or empty'):
            generate(self.model, blocked, 'sample_model')
        with patch('pathlib.Path.write_text', side_effect=OSError('write failed')):
            with self.assertRaisesRegex(OSError, 'write failed'):
                generate(self.model, output, 'sample_model')
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.iterdir()), [])
        with patch('pathlib.Path.rename', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                generate(self.model, output, 'sample_model')
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.iterdir()), [])

    def compile(self, source):
        self.write('main.yambl', source)
        return compile_repository(self.root)

    def test_unsupported_business_features_have_actionable_errors(self):
        cases = [
            ('entity A {}', 'exactly one'),
            ('entity A { id x: string id y: string }', 'exactly one'),
            ('entity A { id x: decimal }', 'exactly one'),
            ('entity A { id x: string attr v: float }', 'unsupported storage type float'),
            ('entity A { id x: string attr v: values<string> }', 'unsupported storage type values'),
            (
                'entity A { id x: string attr t: type<Entity> attr v: self.t::value }',
                'unsupported storage type',
            ),
            (
                "@flags decodes Codes { - 1 One: 'One' } entity A { id x: string attr v: code<Codes> }",
                'unsupported storage type code',
            ),
            ('entity A { id x: string attr id: string }', 'collision'),
            ('entity A { id x: string @dimension(class: string) attr v: string }', 'collision'),
        ]
        for source, message in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, message):
                    build_layout(self.compile(source))
        self.write('left.yambl', 'namespace left entity Same { id x: string }')
        self.write('right.yambl', 'namespace right entity Same { id x: string }')
        with self.assertRaisesRegex(ValueError, 'Python name collision'):
            build_layout(self.compile('entity A { id x: string }'))

    def test_generation_collisions_and_dimension_grouping(self):
        cases = [
            ('entity Reader { id x: string }', 'public-name collision'),
            ('entity Many { id x: string }', 'reader method collision'),
            ('entity A { id x: string } entity AMany { id x: string }', 'reader method collision'),
            (
                "decodes Codes { - 1 None: 'None' } entity A { id x: string attr v: code<Codes> }",
                'enum member',
            ),
            ('entity A { id x: string @temporal(day: date) snapshot attr x: string }', 'duplicate'),
            (
                'entity A { id x: string @temporal(day: date) snapshot attr price: decimal attr price_range: string }',
                'range method collision',
            ),
            (
                'entity A { id x: string @temporal(class: date) snapshot attr price: decimal }',
                'time parameter collision',
            ),
            (
                'entity A { id x: string @dimension(start_date: string) snapshot attr price: decimal }',
                'time parameter collision',
            ),
        ]
        for source, message in cases:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, message):
                layout = build_layout(self.compile(source))
                python_package(layout, 'sample_model')
        model = self.compile('''entity A { id x: string
            @dimension(left: string) attr a: string
            @dimension(right: string) attr b: string
            @dimension(left: string) attr c: string
        }''')
        layout = build_layout(model)
        self.assertEqual(layout.fields['A.a'].table, layout.fields['A.c'].table)
        self.assertNotEqual(layout.fields['A.a'].table, layout.fields['A.b'].table)
        static_names = build_layout(
            self.compile(
                'entity A { id key: string @dimension(start_date: string) @dimension(as_of_date: string) attr text: string }'
            )
        )
        self.assertIn('start_date: str', python_package(static_names, 'sample_model'))
        type_ = layout.fields['A.a'].field.type
        from yambl_compiler.ir.model import Value

        self.assertEqual(
            type_label(replace(type_, exact=Value('string', 'fixed', ''))), 'string = fixed'
        )
        self.assertFalse(supported(replace(type_, dependent=True), layout.entities, model))
        self.assertFalse(
            supported(replace(type_, kind='reference', target='unknown'), layout.entities, model)
        )

    def test_schema_and_generation_cli_reports(self):
        artifact = self.root / 'model.json'
        write_ir(self.model, artifact)
        output = self.root / 'generated'
        args = Arguments(
            path=str(artifact), operation='generate', output=str(output), package='sample_model'
        )
        self.assertTrue(run_generation(args, True).ok)
        self.assertFalse(output.exists())
        self.assertTrue(run_generation(args, False).ok)
        self.assertFalse(run_generation(args, False).ok)
        schema_path = self.root / 'schema.json'
        schema_args = Arguments(path='.', operation='schema', output=str(schema_path))
        self.assertTrue(run_generation(schema_args, True).ok)
        self.assertFalse(schema_path.exists())
        self.assertTrue(run_generation(schema_args, False).ok)
        self.assertEqual(
            json.loads(schema_path.read_text())['$defs']['Model']['properties']['format_revision'][
                'const'
            ],
            2,
        )
        self.assertFalse(run_generation(replace(schema_args, output=''), False).ok)
        self.assertFalse(run_generation(replace(schema_args, output='schema.txt'), False).ok)

    def test_layout_rejects_conflicting_ids_dimensions_and_columns(self):
        declarations = {d.identity: d for d in self.model.definitions}
        other_id = next(e for e in declarations['Other'].effective if e.name == 'number')
        bad_child = replace(declarations['B'], effective=(other_id,))
        broken = replace(
            self.model,
            definitions=tuple(
                bad_child if d.identity == 'B' else d for d in self.model.definitions
            ),
        )
        with self.assertRaisesRegex(ValueError, 'share its root ID'):
            build_layout(broken)
        model = self.compile(
            "@flags decodes Codes { - 1 One: 'One' } entity A { id key: string @dimension(source: code<Codes>) attr value: string }"
        )
        with self.assertRaisesRegex(ValueError, 'unsupported dimension'):
            build_layout(model)
        model = self.compile(
            'entity A { id key: string @dimension(source: string) attr dim_source: string }'
        )
        with self.assertRaisesRegex(ValueError, 'SQL column collision'):
            build_layout(model)
        layout = build_layout(self.model)
        fields = dict(layout.fields)
        fields['duplicate'] = layout.fields['A.text']
        with self.assertRaisesRegex(ValueError, 'method collision'):
            python_package(replace(layout, fields=fields), 'sample_model')

    def test_real_cli_generation_commands(self):
        import contextlib
        import io
        import sys

        from yambl_compiler.application.cli import main

        artifact = self.root / 'model.json'
        write_ir(self.model, artifact)
        # The lclang entrance receives the script filename at argv[1].
        prefix = [sys.executable, str(Path(__file__).parents[2] / 'src/yambl_compiler/__main__.py')]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                main([*prefix, 'schema', '-o', 'output', str(self.root / 'contract.json')]), 0
            )
            self.assertEqual(
                main(
                    [
                        *prefix,
                        'generate',
                        '-o',
                        'path',
                        str(artifact),
                        '-o',
                        'output',
                        str(self.root / 'cli-output'),
                        '-o',
                        'package',
                        'sample_model',
                    ]
                ),
                0,
            )
        self.assertIn('generated:', output.getvalue())
        self.assertTrue((self.root / 'cli-output/schema.md').is_file())
