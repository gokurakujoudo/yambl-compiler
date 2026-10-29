"""Verify generated schema, documentation, imports and publication failures."""

import ast
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Self
from unittest.mock import patch

from tests.storage.test_runtime import SOURCE
from tests.support import FilesystemCase, load_generated
from yambl_compiler.application.cli import Arguments, run_generation
from yambl_compiler.ir import compile_repository, write_ir
from yambl_compiler.ir.codec import encode
from yambl_compiler.storage.generation import generate, python_package
from yambl_compiler.storage.layout import build_layout, quote, supported, type_label
from yambl_compiler.storage.rendering import markdown_cell
from yambl_compiler.storage.value_types import code_type_name, generate_values


class GenerationTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write("main.yambl", SOURCE)
        self.model = compile_repository(self.root)

    def test_subtype_dictionary_python_names_and_empty_enum(self):
        model = self.compile(
            "decodes Empty {} decodes Populated { - 100 Bond: 'Bond' } "
            'entity Security { id key: string attr first: code<Empty> '
            'attr second: code<Populated> }'
        )
        layout = build_layout(model)
        targets = {'Empty': 'core.Empty::subtypes', 'Populated': 'core.Security::subtypes'}
        definitions = tuple(
            (
                replace(d, name=targets[d.identity], identity=targets[d.identity], derived=True)
                if d.identity in targets
                else d
            )
            for d in model.definitions
        )
        fields = dict(layout.fields)
        for identity in ('Security.first', 'Security.second'):
            stored = fields[identity]
            type_ = stored.field.type
            arg = type_.arguments[0]
            type_ = replace(type_, arguments=(replace(arg, target=targets[arg.target]),))
            fields[identity] = replace(stored, field=replace(stored.field, type=type_))
        layout = replace(layout, model=replace(model, definitions=definitions), fields=fields)
        source = python_package(layout, 'sample_model')
        parsed = ast.parse(source)
        for name, expected in [('EmptySubtypesCode', []), ('SecuritySubtypesCode', ['Bond'])]:
            node = next(n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == name)
            namespace = {'StrEnum': StrEnum}
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), '<generated>', 'exec'), namespace
            )
            enum = namespace[name]
            self.assertEqual([member.value for member in enum], expected)
            if expected:
                self.assertEqual(enum('Bond').value, 'Bond')
            else:
                with self.assertRaises(TypeError):
                    enum('Bond')
        self.assertIn("'core.Security::subtypes': SecuritySubtypesCode", source)
        self.assertEqual(code_type_name('core.Security::subtypes'), 'SecuritySubtypesCode')
        self.assertEqual(code_type_name('core.Currencies'), 'CurrenciesCode')
        with self.assertRaisesRegex(ValueError, 'unsupported Python code type name'):
            code_type_name('invalid::other')

        value = next(d for d in model.definitions if d.identity == 'builtins.decodes_entry')
        subtype = fields['Security.second'].field.type
        value = replace(value, fields=(replace(value.fields[0], type=subtype),))
        value_source = generate_values(replace(model, definitions=(value,)))
        self.assertIn('from . import SecuritySubtypesCode', value_source)
        self.assertIn(': SecuritySubtypesCode', value_source)

        entities = dict(layout.entities)
        entities['Security'] = replace(entities['Security'], name='SecuritySubtypesCode')
        with self.assertRaisesRegex(ValueError, 'public-name collision: SecuritySubtypesCode'):
            python_package(replace(layout, entities=entities), 'sample_model')

        conflicting = fields['Security.second']
        arg = conflicting.field.type.arguments[0]
        conflicting_type = replace(
            conflicting.field.type, arguments=(replace(arg, target='core.EmptySubtypes'),)
        )
        fields['Security.second'] = replace(
            conflicting, field=replace(conflicting.field, type=conflicting_type)
        )
        with self.assertRaisesRegex(
            ValueError, 'EmptySubtypesCode.*core.Empty::subtypes.*core.EmptySubtypes'
        ):
            python_package(replace(layout, fields=fields), 'sample_model')

    def test_ordinary_codes_accept_unregistered_extensions_without_caching(self):
        model = self.compile(
            "decodes Codes { - 100 One: 'One' } decodes Empty {} "
            'entity A { id key: string attr first: code<Codes> attr second: code<Empty> '
            'attr children: code<A::subtypes> } entity B inherits A {}'
        )
        layout = build_layout(model)

        def classes(model):
            source = python_package(replace(layout, model=model), 'sample_model')
            parsed = ast.parse(source)
            result = {}
            for name in ('CodesCode', 'EmptyCode', 'ASubtypesCode'):
                node = next(
                    n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == name
                )
                namespace = {'StrEnum': StrEnum, 'Self': Self}
                exec(
                    compile(ast.Module(body=[node], type_ignores=[]), '<generated>', 'exec'),
                    namespace,
                )
                result[name] = namespace[name]
            return result

        types = classes(model)
        code = types['CodesCode']
        self.assertIs(code('One'), code.One)
        self.assertIsInstance(code.One, StrEnum)
        first = code('New')
        second = code('New')
        self.assertEqual(first, 'New')
        self.assertIsInstance(first, code)
        self.assertIsNot(first, second)
        self.assertIsNone(first.name)
        self.assertEqual(list(code), [code.One])
        with self.assertRaises(ValueError):
            code(3)
        self.assertEqual(types['EmptyCode']('New'), 'New')
        self.assertEqual(types['ASubtypesCode'].B, 'B')
        with self.assertRaises(ValueError):
            types['ASubtypesCode']('New')

        output = self.root / 'generated'
        generate(model, output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        self.assertEqual(module.CodesCode('New'), 'New')
        self.assertIs(module.CodesCode('One'), module.CodesCode.One)
        self.assertEqual(module.EmptyCode('New'), 'New')
        with self.assertRaises(ValueError):
            module.ASubtypesCode('New')

        flags = next(d.decorators for d in model.definitions if d.identity == 'builtins.AttrCats')
        definitions = tuple(
            replace(d, decorators=flags) if d.identity == 'Codes' else d for d in model.definitions
        )
        closed = classes(replace(model, definitions=definitions))['CodesCode']
        self.assertEqual(closed.One, 'One')
        with self.assertRaises(ValueError):
            closed('New')

    def test_sqlite_query_plans_use_identity_and_temporal_indexes(self):
        output = self.root / 'indexes'
        generate(self.model, output, 'sample_model')
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            identity = db.execute(
                'EXPLAIN QUERY PLAN SELECT * FROM tb_a WHERE id = ?', ('a',)
            ).fetchall()
            snapshot = db.execute(
                'EXPLAIN QUERY PLAN SELECT * FROM tb_a_price '
                'WHERE id = ? AND dim_source = ? AND date >= ? AND date <= ?',
                ('a', 'X', '2026-01-01', '2026-01-31'),
            ).fetchall()
            scd = db.execute(
                'EXPLAIN QUERY PLAN SELECT * FROM tb_a_outstanding '
                'WHERE id = ? AND valid_from <= ? AND valid_to > ?',
                ('a', '2026-01-31', '2026-01-01'),
            ).fetchall()
        self.assertIn('INDEX', identity[0][-1])
        self.assertIn('id=?', identity[0][-1])
        self.assertIn('INDEX', snapshot[0][-1])
        self.assertIn('date>?', snapshot[0][-1])
        self.assertIn('INDEX', scd[0][-1])
        self.assertIn('id=?', scd[0][-1])

    def test_all_documented_tables_match_sqlite_and_manifest(self):
        output = self.root / "output"
        layout = generate(self.model, output, "sample_model")
        schema = (output / "schema.sql").read_text(encoding="utf-8")
        reference = (output / "schema.md").read_text(encoding="utf-8")
        manifest = json.loads((output / "storage-manifest.json").read_text(encoding="utf-8"))
        diagram = (output / 'schema-relations.puml').read_text(encoding='utf-8')
        self.assertIn(diagram, reference)
        self.assertIn('```plantuml\n', reference)
        self.assertFalse((output / 'schema-relations.mmd').exists())
        nodes = {t.name: 't' + str(i) for i, t in enumerate(layout.tables)}
        for table in layout.tables:
            self.assertIn('entity "' + table.name + '" as ' + nodes[table.name], diagram)
        self.assertIn(' --> ', diagram)
        self.assertIn(' ..> ', diagram)
        python_reference = (output / 'python-schema.md').read_text()
        self.assertIn('Data.open(config_path)', python_reference)
        self.assertIn('check-data -o config repository.lclcfg', python_reference)
        self.assertNotIn('--backend', python_reference)
        self.assertIn('## APriceRecord', (output / 'python-schema.md').read_text())
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(schema)
            self.assertEqual(
                {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                {t.name for t in layout.tables},
            )
            for table in layout.tables:
                self.assertIn("## " + table.name, reference)
                columns = list(db.execute("PRAGMA table_info(" + quote(table.name) + ")"))
                self.assertEqual(
                    [(r[1], r[2], not bool(r[3]), r[4]) for r in columns],
                    [(c.name, c.sql_type, c.nullable, c.default or None) for c in table.columns],
                )
                self.assertEqual(
                    tuple(r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]),
                    table.primary,
                )
                self.assertEqual(
                    len(list(db.execute("PRAGMA foreign_key_list(" + quote(table.name) + ")"))),
                    len(table.primary) - 1 if table.foreign_keys else 0,
                )
                self.assertEqual(
                    {
                        r[2]
                        for r in db.execute('PRAGMA foreign_key_list(' + quote(table.name) + ')')
                    },
                    set(table.parents),
                )
                for _, _, parent, child_col, parent_col, *_ in db.execute(
                    'PRAGMA foreign_key_list(' + quote(table.name) + ')'
                ):
                    self.assertIn(
                        f"{nodes[table.name]}::{child_col} --> {nodes[parent]}::{parent_col} : FK (",
                        diagram,
                    )
                block = diagram.split(' as ' + nodes[table.name] + ' {\n')[1].split('\n}')[0]
                for _, name, sql_type, required, _, pk in columns:
                    nullable = 'NOT NULL' if required else 'NULL'
                    primary = f" <<PK {pk}>>" if pk else ''
                    self.assertIn(f"    {name} : {sql_type} {nullable}{primary}", block)
                for column in table.columns:
                    self.assertIn("| " + column.name + " |", reference)
                for check in table.checks:
                    self.assertIn("CHECK (" + check + ")", reference)
                for i, _ in enumerate(table.indexes):
                    self.assertIn(table.name + "__index_" + str(i), reference)
            self.assertEqual(
                dict(db.execute("SELECT * FROM tb_yambl_metadata")),
                {"layout_revision": "7", "model_digest": self.model.model_digest},
            )
        self.assertEqual(len(manifest["tables"]), len(layout.tables))
        self.assertIn("model default: 20", reference)
        self.assertIn("not SQL defaults", reference)
        self.assertIn("not by a target foreign key", reference)
        self.assertNotEqual(layout.fields["A.limit"].table, layout.fields["A.text"].table)
        config = next(t for t in layout.tables if t.name == "tb_a_config")
        self.assertEqual(config.primary, ("id", "field"))
        self.assertEqual(
            [c.name for c in config.columns],
            ["id", "field", "state", "bool", "int", "decimal"],
        )
        self.assertEqual(layout.fields["A.limit"].table, layout.fields["A.enabled"].table)
        self.assertEqual(layout.fields["A.limit"].key_values, (("field", "A.limit"),))
        self.assertNotEqual(layout.fields["A.price"].table, layout.fields["A.positions"].table)
        self.assertEqual(layout.fields["A.text"].table, layout.fields["A.extra"].table)
        self.assertEqual(markdown_cell("中文|`<>&\r\n"), "中文&#124;&#96;&lt;&gt;&amp;<br>")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            generate(self.model, output, "sample_model")
        self.assertEqual((output / "schema.sql").read_text(encoding="utf-8"), schema)
        other = self.root / "other"
        other.mkdir()
        generate(self.model, other, "sample_model")
        self.assertEqual((other / "schema.md").read_bytes(), (output / "schema.md").read_bytes())
        self.assertEqual(
            (other / "sample_model/__init__.py").read_bytes(),
            (output / "sample_model/__init__.py").read_bytes(),
        )

    def test_dry_run_and_failed_writes_preserve_destination(self):
        output = self.root / "missing" / "generated"
        generate(self.model, output, "sample_model", True)
        self.assertFalse(output.parent.exists())
        for package in ("../bad", "class", ""):
            with self.assertRaisesRegex(ValueError, "package"):
                generate(self.model, output, package)
        blocked = self.write("file", "keep")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            generate(self.model, blocked, "sample_model")
        with patch("pathlib.Path.write_text", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                generate(self.model, output, "sample_model")
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.iterdir()), [])
        with patch("pathlib.Path.rename", side_effect=OSError("publish failed")):
            with self.assertRaisesRegex(OSError, "publish failed"):
                generate(self.model, output, "sample_model")
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.iterdir()), [])

    def test_readable_names_wide_identity_and_collisions(self):
        model = self.compile(
            "namespace core\n            entity Portfolio { id portfolio_id: string attr title: string }\n            entity Fund inherits Portfolio { snapshot attr holdings: collection<HoldingEntry> }\n            entity HoldingEntry { id holding_entry_id: integer }\n            entity Security { dimension dimension_378 { value source: string } dimension dimension_450 { value source: string } dimension dimension_508 { value source: code<'Sources'> } dimension dimension_575 { value source: string } \n                id security_id: string\n                static attr name: string\n                @dimension(dimension_378) snapshot attr price: decimal\n                @dimension(dimension_450) attr a: string\n                @dimension(dimension_508) attr b: string\n                @dimension(dimension_575) attr c: string\n            }\n        "
        )
        layout = build_layout(model)
        self.assertEqual(layout.fields["core.Portfolio.title"].table, "tb_core_portfolio")
        holdings = layout.fields["core.Portfolio.holdings"]
        self.assertEqual(holdings.table, "tb_core_portfolio_holdings_status")
        self.assertEqual(holdings.members, "tb_core_portfolio_holdings")
        self.assertEqual(layout.fields["core.Security.price"].table, "tb_core_security_price")
        self.assertEqual(layout.fields["core.Security.a"].table, "tb_core_security_static_source")
        self.assertEqual(layout.fields["core.Security.b"].table, "tb_core_security_static_source_2")
        self.assertEqual(
            layout.fields["core.Security.c"].table,
            layout.fields["core.Security.a"].table,
        )
        self.assertIn("tb_core_holding_entry", {t.name for t in layout.tables})
        for source in (
            "entity YamblMetadata { id key: string }",
            "entity A { id key: string snapshot attr price: string } entity APrice { id key: string }",
            "entity A { id key: string snapshot attr x: collection<B> snapshot attr x_status: string } entity B { id key: string }",
            "namespace foo entity BarBaz { id key: string }",
            "entity A { id key: string config attr x: string snapshot attr config: string }",
        ):
            with self.subTest(source=source):
                if source.startswith("namespace"):
                    self.write("other.yambl", "namespace foo.bar entity Baz { id key: string }")
                with self.assertRaisesRegex(ValueError, "SQL table collision"):
                    build_layout(self.compile(source))
                (self.root / "other.yambl").unlink(missing_ok=True)

    def test_builtin_storage_and_declared_dictionary_rows(self):
        output = self.root / "output"
        model = self.compile(SOURCE.replace("US dollar", "Dollar\\'s"))
        layout = generate(model, output, "sample_model")
        manifest = json.loads((output / "storage-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["fields"]["builtins.ConfigGroup.when"]["readable"])
        self.assertEqual(manifest["identity_tables"]["builtins.Decodes"], "tb_decodes")
        self.assertEqual(manifest["roots"]["builtins.ConfigGroup"], "builtins.ConfigGroup")
        self.assertTrue(manifest["fields"]["A.text"]["readable"])
        self.assertEqual(layout.fields["A.members"].table, "tb_a")
        self.assertEqual(layout.fields["A.members"].members, "tb_a_members")
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript((output / "schema.sql").read_text(encoding="utf-8"))
            self.assertEqual(
                list(
                    db.execute(
                        "SELECT code, decode FROM tb_decodes_entries WHERE decodes_id = ? ORDER BY code",
                        ("Currencies",),
                    )
                ),
                [("CNY", "人民币"), ("USD", "Dollar's")],
            )
            self.assertEqual(
                dict(
                    db.execute(
                        "SELECT code, ordering FROM tb_decodes_entries WHERE decodes_id = ? AND code IN (?, ?)",
                        ("builtins.AttrCats", "None", "All"),
                    )
                ),
                {"None": "0", "All": "62"},
            )
            self.assertEqual(
                db.execute(
                    "SELECT entries__state FROM tb_decodes WHERE decodes_id = ?",
                    ("Currencies",),
                ).fetchone(),
                ("VALUE",),
            )
            db.execute(
                "INSERT INTO tb_hierarchy (hierarchy_id, hierarchy_entries__state) VALUES ('prices', 'VALUE')"
            )
            db.execute("INSERT INTO tb_hierarchy_entries VALUES ('prices', 'Primary', '1')")
            db.execute(
                "INSERT INTO tb_config_group (config_group_id, target_entity_type__state, target_entity_type, when__state, then_set__state) VALUES ('g', 'VALUE', 'A', 'VALUE', 'VALUE')"
            )
            encoded = json.dumps(
                {"type": encode(layout.fields["A.text"].field.type), "value": "中'文"},
                ensure_ascii=False,
            )
            db.execute(
                "INSERT INTO tb_config_group_when VALUES (?, ?, ?)",
                ("g", "A.text", encoded),
            )
            self.assertEqual(
                json.loads(db.execute("SELECT value FROM tb_config_group_when").fetchone()[0])[
                    "value"
                ],
                "中'文",
            )
            for sql, values in (
                (
                    "INSERT INTO tb_decodes_entries VALUES (?, ?, ?, ?)",
                    ("Currencies", "USD", "duplicate", "2"),
                ),
                (
                    "INSERT INTO tb_hierarchy_entries VALUES (?, ?, ?)",
                    ("prices", "Secondary", "1"),
                ),
                (
                    "INSERT INTO tb_hierarchy_entries VALUES (?, ?, ?)",
                    ("missing", "Primary", "1"),
                ),
                (
                    "INSERT INTO tb_config_group_when VALUES (?, ?, ?)",
                    ("g", "A.text", encoded),
                ),
                (
                    "INSERT INTO tb_config_group_then_set VALUES (?, ?, ?)",
                    ("missing", "A.limit", encoded),
                ),
            ):
                with (
                    self.subTest(sql=sql, values=values),
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    db.execute(sql, values)
            for value in (
                "broken",
                "[]",
                "{}",
                '{"type": "string", "value": "x"}',
                '{"type": {}}',
            ):
                with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT INTO tb_config_group_then_set VALUES (?, ?, ?)",
                        ("g", "A.limit", value),
                    )
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])

    def compile(self, source):
        self.write("main.yambl", source)
        return compile_repository(self.root)

    def test_unsupported_business_features_have_actionable_errors(self):
        cases = [
            ("entity A {}", "exactly one"),
            ("entity A { id x: string id y: string }", "exactly one"),
            ("entity A { id x: decimal }", "exactly one"),
            (
                "entity A { id x: string attr v: float }",
                "unsupported storage type float",
            ),
            (
                "entity A { id x: string attr v: values<string> }",
                "unsupported storage type values",
            ),
            (
                "entity A { id x: string attr t: type<Entity> attr v: self.t::value }",
                "unsupported storage type",
            ),
            (
                "@flags decodes Codes { - 1 One: 'One' } entity A { id x: string attr v: code<Codes> }",
                "unsupported storage type code",
            ),
            ("entity A { id x: string attr id: string }", "collision"),
            (
                "entity A { dimension dimension_24 { value class: string }  id x: string @dimension(dimension_24) attr v: string }",
                "collision",
            ),
        ]
        for source, message in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, message):
                    build_layout(self.compile(source))
        self.write("left.yambl", "namespace left entity Same { id x: string }")
        self.write("right.yambl", "namespace right entity Same { id x: string }")
        with self.assertRaisesRegex(ValueError, "Python name collision"):
            build_layout(self.compile("entity A { id x: string }"))

    def test_generation_collisions_and_dimension_grouping(self):
        cases = [
            ('entity A { id key: string attr reader: string }', 'property collision'),
            (
                'entity A { id key: string attr x: string attr read_x: string }',
                'property collision',
            ),
            (
                "entity A { dimension dimension_26 { value value: string }  id key: string @dimension(dimension_26) attr x: string }",
                'record member collision',
            ),
            (
                "entity A { dimension dimension_26 { value valid_from: string }  id key: string @dimension(dimension_26) scd attr x: string }",
                'record member collision',
            ),
            ("entity Reader { id x: string }", "public-name collision"),
            ("entity Many { id x: string }", "reader method collision"),
            (
                "entity A { id x: string } entity AMany { id x: string }",
                "reader method collision",
            ),
            (
                "decodes Codes { - 1 None: 'None' } entity A { id x: string attr v: code<Codes> }",
                "enum member",
            ),
            (
                "entity A { id x: string @temporal(day: date) snapshot attr x: string }",
                "duplicate",
            ),
            (
                "entity A { id x: string @temporal(day: date) snapshot attr price: decimal attr price_range: string }",
                "range method collision",
            ),
            (
                "entity A { id x: string @temporal(class: date) snapshot attr price: decimal }",
                "time parameter collision",
            ),
            (
                "entity A { dimension dimension_24 { value start_date: string }  id x: string @dimension(dimension_24) snapshot attr price: decimal }",
                "time parameter collision",
            ),
        ]
        for source, message in cases:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, message):
                layout = build_layout(self.compile(source))
                python_package(layout, "sample_model")
        model = self.compile(
            "entity A { dimension dimension_36 { value left: string } dimension dimension_88 { value right: string } dimension dimension_141 { value left: string }  id x: string\n            @dimension(dimension_36) attr a: string\n            @dimension(dimension_88) attr b: string\n            @dimension(dimension_141) attr c: string\n        }"
        )
        layout = build_layout(model)
        self.assertEqual(layout.fields["A.a"].table, layout.fields["A.c"].table)
        self.assertNotEqual(layout.fields["A.a"].table, layout.fields["A.b"].table)
        static_names = build_layout(
            self.compile(
                "entity A { dimension dimension_26 { value start_date: string } dimension dimension_57 { value as_of_date: string }  id key: string @dimension(dimension_26) @dimension(dimension_57) attr text: string }"
            )
        )
        self.assertIn("start_date: str", python_package(static_names, "sample_model"))
        type_ = layout.fields["A.a"].field.type
        from yambl_compiler.ir.model import Value

        self.assertEqual(
            type_label(replace(type_, exact=Value("string", "fixed", ""))),
            "string = fixed",
        )
        self.assertFalse(supported(replace(type_, dependent=True), layout.entities, model))
        self.assertEqual(
            type_label(
                replace(
                    type_,
                    exact=Value("flags", "", "builtins.AttrCats", ("Static",), "2"),
                )
            ),
            "string = builtins.AttrCats[Static; mask=2]",
        )
        self.assertEqual(
            type_label(replace(type_, exact=Value("flags", "", "builtins.AttrCats", (), "0"))),
            "string = builtins.AttrCats[None; mask=0]",
        )
        self.assertFalse(
            supported(
                replace(type_, kind="reference", target="unknown"),
                layout.entities,
                model,
            )
        )

    def test_schema_and_generation_cli_reports(self):
        artifact = self.root / "model.json"
        write_ir(self.model, artifact)
        output = self.root / "generated"
        args = Arguments(
            path=str(artifact),
            operation="generate",
            output=str(output),
            package="sample_model",
        )
        self.assertTrue(run_generation(args, True).ok)
        self.assertFalse(output.exists())
        self.assertTrue(run_generation(args, False).ok)
        self.assertFalse(run_generation(args, False).ok)
        schema_path = self.root / "schema.json"
        schema_args = Arguments(path=".", operation="schema", output=str(schema_path))
        self.assertTrue(run_generation(schema_args, True).ok)
        self.assertFalse(schema_path.exists())
        self.assertTrue(run_generation(schema_args, False).ok)
        self.assertEqual(
            json.loads(schema_path.read_text(encoding="utf-8"))["$defs"]["Model"]["properties"][
                "format_revision"
            ]["const"],
            8,
        )
        self.assertFalse(run_generation(replace(schema_args, output=""), False).ok)
        self.assertFalse(run_generation(replace(schema_args, output="schema.txt"), False).ok)

    def test_layout_rejects_conflicting_ids_dimensions_and_columns(self):
        declarations = {d.identity: d for d in self.model.definitions}
        other_id = next(e for e in declarations["Other"].effective if e.name == "number")
        bad_child = replace(declarations["B"], effective=(other_id,))
        broken = replace(
            self.model,
            definitions=tuple(
                bad_child if d.identity == "B" else d for d in self.model.definitions
            ),
        )
        with self.assertRaisesRegex(ValueError, "share its root ID"):
            build_layout(broken)
        model = self.compile(
            "@flags decodes Codes { - 1 One: 'One' } entity A { dimension dimension_66 { value source: code<Codes> }  id key: string @dimension(dimension_66) attr value: string }"
        )
        with self.assertRaisesRegex(ValueError, "unsupported dimension"):
            build_layout(model)
        model = self.compile(
            "entity A { dimension dimension_26 { value source: string }  id key: string @dimension(dimension_26) attr dim_source: string }"
        )
        with self.assertRaisesRegex(ValueError, "SQL column collision"):
            build_layout(model)
        layout = build_layout(self.model)
        fields = dict(layout.fields)
        fields["duplicate"] = layout.fields["A.text"]
        with self.assertRaisesRegex(ValueError, "method collision"):
            python_package(replace(layout, fields=fields), "sample_model")

    def test_real_cli_generation_commands(self):
        import contextlib
        import io
        import sys

        from yambl_compiler.application.cli import main

        artifact = self.root / "model.json"
        write_ir(self.model, artifact)
        # The lclang entrance receives the script filename at argv[1].
        prefix = [
            sys.executable,
            str(Path(__file__).parents[2] / "src/yambl_compiler/__main__.py"),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                main(
                    [
                        *prefix,
                        "schema",
                        "-o",
                        "output",
                        str(self.root / "contract.json"),
                    ]
                ),
                0,
                output.getvalue(),
            )
            self.assertEqual(
                main(
                    [
                        *prefix,
                        "generate",
                        "-o",
                        "path",
                        str(artifact),
                        "-o",
                        "output",
                        str(self.root / "cli-output"),
                        "-o",
                        "package",
                        "sample_model",
                    ]
                ),
                0,
                output.getvalue(),
            )
        self.assertIn("generated:", output.getvalue())
        self.assertTrue((self.root / "cli-output/schema.md").is_file())
