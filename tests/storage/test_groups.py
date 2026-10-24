"""Verify grouped static storage through generated SQLite and typed readers."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository, load_ir, write_ir
from yambl_compiler.storage.generation import generate, python_package
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.rendering import ddl
from yambl_compiler.storage.results import EntityRef, Status, Success

SOURCE = """
namespace core
type amount = decimal
entity Security { id key: string attr label: string }
entity Bond inherits Security {
    @sparse('bond_info') attr coupon: amount
    @sparse(cat='bond_info') attr active: bool
    @sparse('bond_info') attr count: integer
    @sparse('bond_info') attr issue_date: date
    @sparse('bond_info') attr code: code<'Codes'>
    @sparse('bond_info') attr peer: Issuer
    @sparse('bond_info') attr reference: Security
    @sparse('single') attr serial: snowflake_id
    @dense('details') attr name: string
    @dense('details') attr enabled: bool
}
entity Child inherits Bond { @override attr coupon: amount }
extend Bond { @dense('details') attr notes: string }
entity Issuer { id number: integer }
"""


class GroupTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write("main.yambl", SOURCE)
        self.model = compile_repository(self.root)

    def test_dictionary_names_cannot_select_storage_layout(self):
        self.write(
            "main.yambl",
            "entity A { id key: string @dimension(source: code<'x:sparse:y'>) attr quote: string }",
        )
        model = compile_repository(self.root)
        layout = build_layout(model)
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(ddl(layout))
            self.assertEqual(
                [r[1] for r in db.execute("PRAGMA table_info(tb_a_static_source)")],
                ["id", "dim_source", "quote__state", "quote"],
            )
        with self.assertRaisesRegex(ValueError, "unsupported Python code type name"):
            generate(model, self.root / "invalid", "sample_model")
        self.assertFalse((self.root / "invalid").exists())

    def test_sparse_dense_schema_and_reads(self):
        artifact = self.root / "model.json"
        write_ir(self.model, artifact)
        self.assertEqual(load_ir(artifact), self.model)
        output = self.root / "generated"
        layout = generate(self.model, output, "sample_model")
        manifest = json.loads((output / "storage-manifest.json").read_text())
        reference = (output / "schema.md").read_text(encoding="utf-8")
        tables = {t.name: t for t in layout.tables}
        sparse = tables["tb_core_security_bond_info"]
        dense = tables["tb_core_security_details"]
        self.assertEqual(
            set(c.name for c in sparse.columns),
            {"id", "field", "state", "string", "bool", "int", "decimal", "date"},
        )
        self.assertEqual(sparse.primary, ("id", "field"))
        self.assertEqual(
            set(c.name for c in dense.columns),
            {
                "id",
                "name",
                "name__state",
                "enabled",
                "enabled__state",
                "notes",
                "notes__state",
            },
        )
        self.assertEqual(dense.primary, ("id",))
        self.assertEqual(layout.fields["core.Security.coupon"].table, sparse.name)
        self.assertEqual(layout.fields["core.Security.peer"].value, "int")
        self.assertEqual(layout.fields["core.Security.reference"].value, "string")
        self.assertEqual(layout.fields["core.Security.serial"].value, "int")
        self.assertEqual(
            manifest["fields"]["core.Security.coupon"]["key_values"],
            [["field", "core.Security.coupon"]],
        )
        for table in (sparse, dense):
            self.assertIn("## " + table.name, reference)
            for column in table.columns:
                self.assertIn("| " + column.name + " |", reference)
            for check in table.checks:
                self.assertIn(check, reference)
        path = self.root / "example.db"
        with closing(sqlite3.connect(path)) as db:
            db.executescript((output / "schema.sql").read_text())
            for table in (sparse, dense):
                self.assertEqual(
                    [r[1] for r in db.execute("PRAGMA table_info(" + table.name + ")")],
                    [c.name for c in table.columns],
                )
            db.executemany(
                "INSERT INTO tb_core_security(id, entity_type) VALUES (?, ?)",
                [("s", "core.Security"), ("b", "core.Bond"), ("c", "core.Child")],
            )
            values = [
                ("coupon", "decimal", "12345678901234567890.0123456789"),
                ("active", "bool", 1),
                ("count", "int", "123456789012345678901234567890"),
                ("issue_date", "date", "2026-10-24"),
                ("code", "string", "External"),
                ("peer", "int", "99"),
                ("reference", "string", "missing"),
            ]
            for name, column, value in values:
                db.execute(
                    f'INSERT INTO {sparse.name}(id, field, state, "{column}") VALUES (?, ?, ?, ?)',
                    ("b", "core.Security." + name, "VALUE", value),
                )
            db.execute(
                "INSERT INTO tb_core_security_details(id, name__state, name) VALUES ('b', 'VALUE', 'Bond')"
            )
            db.execute(
                "INSERT INTO tb_core_security_details(id, notes__state, enabled__state) VALUES ('c', 'EMPTY', 'NOT_APPLICABLE')"
            )
            db.execute(
                "INSERT INTO tb_core_security_bond_info(id, field, state) VALUES ('c', 'core.Security.coupon', 'EMPTY')"
            )
            for sql in (
                "UPDATE tb_core_security_bond_info SET string = 'extra' WHERE field = 'core.Security.coupon'",
                "UPDATE tb_core_security_bond_info SET decimal = NULL WHERE field = 'core.Security.coupon' AND state = 'VALUE'",
                "UPDATE tb_core_security_bond_info SET state = 'EMPTY' WHERE state = 'VALUE'",
                "UPDATE tb_core_security_bond_info SET bool = 2 WHERE field = 'core.Security.active'",
                "UPDATE tb_core_security_bond_info SET field = 'unknown'",
                "UPDATE tb_core_security_bond_info SET state = 'ERROR'",
                "UPDATE tb_core_security_details SET name = NULL WHERE name__state = 'VALUE'",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
            db.commit()
        module = load_generated(output / "sample_model", "sample_model")
        opened = module.SampleReader.open(path)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        self.addCleanup(reader.close)
        b = reader.get_security("b").value
        self.assertIsInstance(b, module.Bond)
        self.assertEqual(b.read_coupon(), Success(Decimal("12345678901234567890.0123456789")))
        self.assertEqual(b.read_active(), Success(True))
        self.assertEqual(b.read_count(), Success(123456789012345678901234567890))
        self.assertEqual(b.read_issue_date(), Success(date(2026, 10, 24)))
        self.assertEqual(b.read_code(), Success("External"))
        self.assertEqual(b.read_peer(), Success(EntityRef("core.Issuer", 99)))
        self.assertEqual(reader.resolve(b.read_reference().value).status, Status.ENTITY_NOT_FOUND)
        self.assertEqual(b.read_name(), Success("Bond"))
        self.assertEqual(b.read_enabled().status, Status.NO_ENTRY)
        self.assertEqual(b.read_serial().status, Status.NO_ENTRY)
        self.assertEqual(reader.get_child("c").value.read_coupon().status, Status.EMPTY)
        self.assertEqual(reader.get_child("c").value.read_enabled().status, Status.NOT_APPLICABLE)
        self.assertEqual(reader.get_security("s").value.read_coupon().status, Status.NOT_DEFINED)

    def test_group_collisions_and_shared_overloads(self):
        source = "entity A { id key: string } entity B inherits A { @overload @sparse('info') attr x: string } entity C inherits A { @overload attr x: string }"
        self.write("main.yambl", source)
        model = compile_repository(self.root)
        layout = build_layout(model)
        self.assertEqual(layout.fields["A.x"].table, "tb_a_info")
        self.assertEqual(layout.fields["A.x"].key_values, (("field", "A.x"),))
        plain = source.replace("@sparse('info') ", "")
        self.write("main.yambl", plain)
        self.assertEqual(
            [
                l
                for l in python_package(layout, "sample_model").splitlines()
                if "contract_digest =" not in l
            ],
            [
                l
                for l in python_package(
                    build_layout(compile_repository(self.root)), "sample_model"
                ).splitlines()
                if "contract_digest =" not in l
            ],
        )
        declaration = next(d for d in model.definitions if d.identity == "B")
        field = declaration.fields[0]
        call = next(c for c in field.decorators if c.target == "builtins.sparse")
        alternate = replace(call, target="builtins.dense")
        invalid = replace(
            model,
            definitions=tuple(
                (
                    replace(
                        d,
                        fields=(replace(field, decorators=(*field.decorators, alternate)),),
                    )
                    if d.identity == "B"
                    else d
                )
                for d in model.definitions
            ),
        )
        with self.assertRaisesRegex(ValueError, "conflicting storage groups"):
            build_layout(invalid)
        for source in (
            "entity A { id key: string @sparse('info') attr x: string @dense('info') attr y: string }",
            "entity A { id key: string @sparse('config') attr x: string config attr y: string }",
            "entity A { id key: string @dense('x') attr y: string snapshot attr x: string }",
        ):
            self.write("main.yambl", source)
            with self.subTest(source=source), self.assertRaises(ValueError):
                generate(
                    compile_repository(self.root),
                    self.root / "must-not-exist",
                    "sample_model",
                )
            self.assertFalse((self.root / "must-not-exist").exists())
