"""Compare lazy immutable reads across physical storage backends."""

import hashlib
import json
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.storage.test_runtime import RuntimeTests
from yambl_compiler.storage.backends import (
    FileBackend,
    repository_contents,
    validate_row,
)
from yambl_compiler.storage.results import Status, Success


class DualTests(RuntimeTests):
    """Reuse the seeded SQLite fixture for backend parity assertions."""

    def files(self):
        self.writer.row_factory = sqlite3.Row
        rows = {
            t.name: [dict(r) for r in self.writer.execute(f'SELECT * FROM "{t.name}"')]
            for t in self.layout.tables
        }
        path = self.root / "files"
        path.mkdir(exist_ok=True)
        for name, text in repository_contents(self.layout, rows).items():
            file = path / name
            file.parent.mkdir(exist_ok=True)
            file.write_text(text, encoding="utf-8", newline="\n")
        return path

    def test_properties_cache_all_coordinates_and_survive_close(self):
        self.put("A.text", "hello")
        self.put("A.price", "2.50", date="2026-01-02", dim_source="X")
        self.put("A.price", "9", date="2026-01-03", dim_source="Y")
        self.put("A.outstanding", "7", valid_from="2026-01-01", valid_to="2026-01-03")
        self.put("A.positions", date="2026-01-02")
        self.put("A.owners", valid_from="1900-01-01", valid_to="2999-12-31")
        path = self.files()
        for reader in (self.open(), self.module.SampleReader.open_files(path).value):
            self.addCleanup(reader.close)
            a = reader.get_a("a").value
            self.assertEqual(a.text, Success("hello"))
            with self.assertRaises(FrozenInstanceError):
                a.id = "changed"
            self.assertNotEqual(a, self.module.A(reader, "A", "a"))
            prices = a.price
            self.assertEqual(
                [(r.day, str(r.source), r.value.value) for r in prices.value],
                [
                    (date(2026, 1, 2), "X", Decimal("2.50")),
                    (date(2026, 1, 3), "Y", Decimal(9)),
                ],
            )
            with self.assertRaises(FrozenInstanceError):
                prices.value[0].source = "Y"
            self.assertEqual(a.outstanding.value[0].valid_to, date(2026, 1, 3))
            self.assertEqual(a.positions.value[0].value, Success(frozenset()))
            self.assertEqual(a.owners.value[0].valid_from, date(1900, 1, 1))
            self.assertEqual(reader.get_a("b").value.price, Success(()))
            self.assertEqual(a.extra.status, Status.NOT_DEFINED)
            self.assertIs(a.price, prices)
            reader.close()
            self.assertEqual(a.text, Success("hello"))
            self.assertIs(a.price, prices)
            self.assertEqual(
                a.read_price(date(2026, 1, 2), source=self.module.SourcesCode("X")),
                Success(Decimal("2.50")),
            )
            interval = a.read_outstanding_range(date(2026, 1, 1), date(2026, 1, 4))
            self.assertEqual(interval.value[-1][1].status, Status.NO_ENTRY)
            interval.value.clear()
            self.assertEqual(
                len(a.read_outstanding_range(date(2026, 1, 1), date(2026, 1, 4)).value),
                2,
            )
            self.assertEqual(a.read_count().status, Status.ERROR)
            self.assertEqual(a.read_positions(date(2026, 1, 2)), Success(frozenset()))

    def test_lazy_loading_changes_and_immutable_prior_cache(self):
        self.put("A.text", "saved")
        path = self.files()
        reader = self.module.SampleReader.open_files(path).value
        self.addCleanup(reader.close)
        backend = reader._backend
        self.assertEqual(backend.cache, {})
        a = reader.get_a("a").value
        self.assertEqual(set(backend.cache), {"tb_a"})
        self.assertEqual(a.text, Success("saved"))
        self.assertEqual(a.read_text(), Success("saved"))
        file = path / "tables/tb_a_config.jsonl"
        before = file.read_bytes()
        file.write_bytes(before + b"\n")
        self.assertEqual(a.read_limit().status, Status.ERROR)
        file.write_bytes(before)
        self.assertEqual(a.read_limit().status, Status.ERROR)
        self.assertEqual(a.text, Success("saved"))
        reader.close()
        fresh = self.module.SampleReader.open_files(path).value
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.get_a("a").value.read_limit(), Success(20, "model_default"))
        file.unlink()
        self.assertEqual(fresh.get_a("a").status, Status.ERROR)
        self.assertTrue(fresh._backend.failed)

    def test_file_metadata_hash_and_row_failures(self):
        path = self.files()
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text())
        for change in (
            [],
            {},
            {**metadata, "format_revision": True},
            {**metadata, "format_revision": 9},
            {**metadata, "format_revision": 1},
            {**metadata, "layout_revision": True},
            {**metadata, "layout_revision": 3},
            {**metadata, "layout_revision": 5},
            {**metadata, "model_digest": "bad"},
            {**metadata, "tables": []},
            {**metadata, "tables": {}},
            {**metadata, "tables": {**metadata["tables"], "tb_a": "z" * 64}},
            {**metadata, "tables": {**metadata["tables"], "tb_a": 5}},
            {**metadata, "tables": {**metadata["tables"], "tb_a": "short"}},
        ):
            metadata_file.write_text(json.dumps(change))
            self.assertEqual(self.module.SampleReader.open_files(path).status, Status.ERROR)
        metadata_file.write_text(json.dumps(metadata))
        file = path / "tables/tb_a.jsonl"
        original = file.read_bytes()
        file.write_bytes(original + b"\n")
        reader = self.module.SampleReader.open_files(path).value
        self.assertIn("digest mismatch", reader.get_a("a").message)
        reader.close()
        row = json.loads(original.splitlines()[0])
        variants = [
            "[]",
            '{"id":"a","id":"b"}',
            '{"id":NaN}',
            json.dumps({**row, "unknown": 1}),
            json.dumps({**row, "text__state": "ERROR"}),
            json.dumps({**row, "id": None}),
            json.dumps({**row, "id": 1}),
            json.dumps(row) + "\n" + json.dumps(row),
        ]
        for content in variants:
            file.write_text(content + "\n", encoding="utf-8", newline="\n")
            metadata["tables"]["tb_a"] = hashlib.sha256(file.read_bytes()).hexdigest()
            metadata_file.write_text(json.dumps(metadata))
            reader = self.module.SampleReader.open_files(path).value
            self.addCleanup(reader.close)
            result = reader.get_a("a")
            self.assertEqual(result.status, Status.ERROR)
            self.assertIn("tb_a.jsonl:", result.message)
        with patch.object(Path, "is_symlink", return_value=True):
            self.assertEqual(self.module.SampleReader.open_files(path).status, Status.ERROR)

    def test_physical_lanes_and_time_coordinates(self):
        self.put("A.limit", "10")
        self.put("A.enabled", 0)
        self.put("A.price", "5", date="2026-01-01", dim_source="X")
        self.put("A.outstanding", "5", valid_from="2026-01-01", valid_to="2026-01-02")
        self.writer.row_factory = sqlite3.Row
        tables = {t.name: t for t in self.layout.tables}
        config = dict(
            self.writer.execute("SELECT * FROM tb_a_config WHERE field='A.limit'").fetchone()
        )
        boolean = dict(
            self.writer.execute("SELECT * FROM tb_a_config WHERE field='A.enabled'").fetchone()
        )
        price = dict(self.writer.execute("SELECT * FROM tb_a_price").fetchone())
        scd = dict(self.writer.execute("SELECT * FROM tb_a_outstanding").fetchone())
        for table, row in [
            ("tb_a_config", {**config, "field": "bad"}),
            ("tb_a_config", {**config, "int": None}),
            ("tb_a_config", {**config, "bool": 1}),
            ("tb_a_config", {**boolean, "bool": 2}),
            ("tb_a_price", {**price, "date": "20260101"}),
            ("tb_a_outstanding", {**scd, "valid_to": "2026-01-01"}),
            ("tb_a_outstanding", {**scd, "valid_from": "20260101"}),
        ]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_row(self.layout, tables[table], row)
        for table, row in [
            ("tb_a_config", config),
            ("tb_a_config", boolean),
            ("tb_a_price", price),
            ("tb_a_outstanding", scd),
        ]:
            validate_row(self.layout, tables[table], row)
        file = self.files()
        backend = FileBackend(file, self.layout)
        self.assertEqual(backend.rows("tb_a_config", {"field": "A.limit"})[0]["int"], "10")
        self.assertEqual(backend.rows("tb_a_config", {"field": "missing"}), [])
        backend.close()
        mapping = tables['tb_config_group_when']
        for value in ('[]', '{}', '{"type":{}}', '{"type":"string","value":1}'):
            with self.assertRaises(ValueError):
                validate_row(
                    self.layout,
                    mapping,
                    {'config_group_id': 'g', 'field': 'A.limit', 'value': value},
                )
        validate_row(
            self.layout,
            mapping,
            {'config_group_id': 'g', 'field': 'A.limit', 'value': '{"type":{},"value":"1"}'},
        )

    def test_property_corruption_and_failed_queries_are_not_cached(self):
        self.put("A.price", "broken", date="2026-01-01", dim_source="X")
        reader = self.open()
        a = reader.get_a("a").value
        self.assertEqual(a.price.value[0].value.status, Status.ERROR)
        with patch.object(reader._backend, "rows", side_effect=OSError("unreadable")):
            self.assertEqual(a.price.status, Status.ERROR)
            self.assertEqual(a.read_text().status, Status.ERROR)
        self.assertEqual(a.read_text().status, Status.NO_ENTRY)
        with patch.object(
            reader._backend,
            "rows",
            return_value=[{"dim_source": "X", "date": "20260101"}],
        ):
            self.assertEqual(a.price.status, Status.ERROR)
        with patch.object(
            reader._backend,
            "rows",
            return_value=[{"valid_from": "2026-01-03", "valid_to": "2026-01-01"}],
        ):
            self.assertEqual(a.outstanding.status, Status.ERROR)
        row = {
            "dim_source": "X",
            "date": "2026-01-01",
            "price__state": "VALUE",
            "price": "2",
        }
        with patch.object(reader._backend, "rows", return_value=[row, row]):
            self.assertEqual(a.price.status, Status.ERROR)
        reader.close()
        self.assertEqual(a.price.status, Status.ERROR)
