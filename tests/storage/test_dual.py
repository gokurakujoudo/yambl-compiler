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
    Selection,
    repository_contents,
    validate_row,
)
from yambl_compiler.storage.results import EntityRef, Status, Success


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

    def test_hidden_type_fields_and_builtin_identity_on_both_backends(self):
        self.writer.execute("INSERT INTO tb_hierarchy (hierarchy_id) VALUES ('h')")
        self.writer.execute("INSERT INTO tb_config_group (config_group_id) VALUES ('g')")
        self.writer.commit()
        path = self.files()
        for reader in (self.open(), self.module.SampleReader.open_files(path).value):
            self.addCleanup(reader.close)
            child = reader.get_a('b').value
            self.assertEqual(child.read_entity_type(), Success('B'))
            self.assertEqual(child.read_root_entity_type(), Success('A'))
            self.assertEqual(child.read_actual_type(), Success('B', 'computation'))
            self.assertEqual(child.read_root_type(), Success('A', 'computation'))
            reference = EntityRef('builtins.Decodes', 'Currencies')
            self.assertEqual(reader._batch.entities([reference]), (Success(reference),))
            dictionary = reader.get_decodes('Currencies').value
            self.assertEqual(dictionary.read_entity_type(), Success('builtins.Decodes'))
            self.assertEqual(dictionary.read_root_entity_type(), Success('builtins.Decodes'))
            for handle, name in (
                (reader.get_hierarchy('h').value, 'builtins.Hierarchy'),
                (reader.get_config_group('g').value, 'builtins.ConfigGroup'),
            ):
                self.assertEqual(handle.read_entity_type(), Success(name))
                self.assertEqual(handle.read_root_entity_type(), Success(name))

    def test_extended_codes_follow_each_reader_snapshot(self):
        self.put('A.currency', 'CNY')
        self.writer.execute('PRAGMA journal_mode=WAL')
        old_files = self.files()
        old_sql = self.open()
        old_file = self.module.SampleReader.open_files(old_files).value
        self.addCleanup(old_file.close)
        old_handles = [old_sql.get_a('a').value, old_file.get_a('a').value]
        self.writer.execute(
            "INSERT INTO tb_decodes_entries (decodes_id, code, decode, ordering) VALUES (?, ?, ?, ?)",
            ('Currencies', 'EUR', 'Euro', '300'),
        )
        self.writer.execute('UPDATE tb_a SET currency = ? WHERE id = ?', ('EUR', 'a'))
        self.writer.commit()
        self.writer.row_factory = sqlite3.Row
        rows = {
            t.name: [dict(r) for r in self.writer.execute(f'SELECT * FROM "{t.name}"')]
            for t in self.layout.tables
        }
        new_files = self.root / 'new-files'
        new_files.mkdir()
        for name, text in repository_contents(self.layout, rows).items():
            file = new_files / name
            file.parent.mkdir(exist_ok=True)
            file.write_text(text, encoding='utf-8', newline='\n')
        new_sql = self.open()
        new_file = self.module.SampleReader.open_files(new_files).value
        self.addCleanup(new_file.close)
        for handle in old_handles:
            self.assertIs(handle.read_currency().value, self.module.CurrenciesCode.CNY)
            with self.assertRaisesRegex(ValueError, 'unknown code'):
                handle.reader._decode(self.layout.fields['A.currency'].field.type, 'EUR')
        for reader in (new_sql, new_file):
            result = reader.get_a('a').value.read_currency()
            self.assertIsInstance(result.value, self.module.CurrenciesCode)
            self.assertEqual(str(result.value), 'EUR')
            self.assertIsNone(result.value.name)
            self.assertEqual(result.value.value, 'EUR')
            another = self.module.CurrenciesCode('EUR')
            self.assertEqual(result.value, another)
            self.assertIsNot(result.value, another)
            self.assertIs(self.module.CurrenciesCode('CNY'), self.module.CurrenciesCode.CNY)
            self.assertTrue(reader.validate().value.valid)

    def test_dictionary_integrity_and_invalid_code_are_reported(self):
        self.put('A.currency', 'UNKNOWN')
        self.writer.execute(
            "UPDATE tb_decodes_entries SET decode = 'changed' WHERE decodes_id = 'Currencies' AND code = 'USD'"
        )
        self.writer.execute(
            "INSERT INTO tb_decodes_entries (decodes_id, code, decode, ordering) VALUES (?, ?, ?, ?)",
            ('Other::subtypes', 'Fake', 'Fake', '100'),
        )
        self.writer.commit()
        path = self.files()
        for reader in (self.open(), self.module.SampleReader.open_files(path).value):
            self.addCleanup(reader.close)
            report = reader.validate().value
            self.assertFalse(report.valid)
            messages = [issue.message for issue in report.issues]
            self.assertTrue(any('model dictionary entry changed: USD' in m for m in messages))
            self.assertTrue(any('read-only dictionary entries changed' in m for m in messages))
            self.assertTrue(any('unknown code for Currencies' in m for m in messages))
            selected = reader.validate_dictionary_references({'Currencies'})
            self.assertTrue(
                any('unknown code for Currencies' in i.message for i in selected.issues)
            )

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
        self.assertEqual(backend.cache, {})
        self.assertEqual(set(backend.offsets), {"tb_a"})
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

    def test_persistent_offset_index_and_legacy_fallback(self):
        self.put("A.text", "中文")
        self.put("A.price", "2.50", date="2026-01-02", dim_source="X")
        self.put("A.price", "9", date="2026-01-03", dim_source="X")
        path = self.files()
        backend = FileBackend(path, self.layout)
        self.addCleanup(backend.close)
        matches = backend.rows_many(
            "tb_a_price",
            [Selection((("id", "a"), ("dim_source", "X")), "snapshot", "2026-01-03", "2026-01-03")],
        )
        self.assertEqual([r["price"] for r in matches[0]], ["9"])
        self.assertNotIn("tb_a_price", backend.cache)
        self.assertIn("tb_a_price", backend.offsets)
        selection = Selection(
            (("id", "a"), ("dim_source", "X")), "snapshot", "2026-01-03", "2026-01-03"
        )
        with patch.object(backend, "_read_offsets", wraps=backend._read_offsets) as reads:
            repeated = backend.rows_many("tb_a_price", [selection, selection])
        self.assertEqual(len(reads.call_args.args[1]), 1)
        self.assertEqual(repeated[0], repeated[1])
        repeated[0][0]["price"] = "changed"
        self.assertEqual(repeated[1][0]["price"], "9")
        self.assertEqual(backend.rows("tb_a", {"id": "a"})[0]["text"], "中文")
        self.assertEqual(backend.rows("tb_a", {"id": "b"})[0]["id"], "b")
        self.assertEqual(backend.rows("tb_a", {"id": "missing"}), [])
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        del metadata["indexes"]
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        legacy = FileBackend(path, self.layout)
        self.addCleanup(legacy.close)
        self.assertEqual(legacy.rows("tb_a", {"id": "a"})[0]["id"], "a")
        self.assertEqual(legacy.rows("tb_a", {"id": "a"})[0]["id"], "a")
        self.assertIn("tb_a", legacy.cache)

    def test_temporal_index_and_scan_batches_do_not_share_incompatible_cache_rows(self):
        self.put("A.price", "2.50", date="2026-01-02", dim_source="X")
        self.put("A.outstanding", "7", valid_from="2026-01-01", valid_to="2026-01-03")
        path = self.files()
        cases = (
            (
                "A.price",
                (("id", "a"), ("dim_source", "X")),
                "snapshot",
                "2026-01-02",
                "2026-01-02",
                "2.50",
            ),
            (
                "A.outstanding",
                (("id", "a"),),
                "scd",
                "2026-01-02",
                "2026-01-02",
                "7",
            ),
        )
        for identity, keys, temporal, start, end, value in cases:
            stored = self.layout.fields[identity]
            indexed = Selection(keys, temporal, start, end)
            scanned = Selection(((stored.state, "VALUE"),), temporal, start, end)
            columns = ("id", stored.value)
            expected = [[{"id": "a", stored.value: value}]]
            for indexed_first in (True, False):
                with self.subTest(identity=identity, indexed_first=indexed_first):
                    backend = FileBackend(path, self.layout)
                    self.addCleanup(backend.close)
                    first = [indexed] if indexed_first else [indexed, scanned]
                    second = [indexed, scanned] if indexed_first else [indexed]
                    self.assertEqual(
                        backend.rows_many(stored.table, first, columns), expected * len(first)
                    )
                    self.assertEqual(
                        backend.rows_many(stored.table, second, columns), expected * len(second)
                    )

    def test_persistent_index_detects_missing_and_unselected_corruption(self):
        path = self.files()
        index_file = path / "indexes/tb_a.jsonl"
        original = index_file.read_bytes()
        index_file.write_bytes(original + b" ")
        backend = FileBackend(path, self.layout)
        self.addCleanup(backend.close)
        with self.assertRaisesRegex(ValueError, "index digest mismatch"):
            backend.rows("tb_a", {"id": "a"})
        index_file.write_bytes(original)
        file = path / "tables/tb_a.jsonl"
        records = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
        records[1]["text__state"] = "ERROR"
        records_text = "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in records
        )
        # Rebuild offsets and hashes, as a damaged but self-consistent archive could.
        contents = repository_contents(
            self.layout, {t.name: records if t.name == "tb_a" else [] for t in self.layout.tables}
        )
        file.write_text(records_text, encoding="utf-8", newline="\n")
        index_file.write_text(contents["indexes/tb_a.jsonl"], encoding="utf-8", newline="\n")
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["tables"]["tb_a"] = hashlib.sha256(file.read_bytes()).hexdigest()
        metadata["indexes"]["tb_a"] = hashlib.sha256(index_file.read_bytes()).hexdigest()
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        corrupt = FileBackend(path, self.layout)
        self.addCleanup(corrupt.close)
        with self.assertRaisesRegex(ValueError, "invalid stored field state"):
            corrupt.rows("tb_a", {"id": "a"})

    def test_persistent_index_rejects_invalid_records_and_missing_file(self):
        path = self.files()
        index_file = path / "indexes/tb_a.jsonl"
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        original = index_file.read_text(encoding="utf-8")
        item = json.loads(original.splitlines()[0])
        variants = [
            "{}\n",
            "[]\n",
            json.dumps([[], None, 0, item[-1]]) + "\n",
            json.dumps([item[0], 3, 0, item[-1]]) + "\n",
            json.dumps([item[0], None, "0", item[-1]]) + "\n",
            json.dumps([item[0], None, 0, 0]) + "\n",
            json.dumps([item[0], None, 1, item[-1]]) + "\n",
            json.dumps(item) + "\n" + json.dumps([item[0], None, item[-1], item[-1]]) + "\n",
            "",
        ]
        for content in variants:
            with self.subTest(content=content):
                index_file.write_text(content, encoding="utf-8", newline="\n")
                metadata["indexes"]["tb_a"] = hashlib.sha256(index_file.read_bytes()).hexdigest()
                metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
                backend = FileBackend(path, self.layout)
                with self.assertRaises(ValueError):
                    backend.rows("tb_a", {"id": "a"})
                backend.close()
        index_file.unlink()
        with self.assertRaises(FileNotFoundError):
            FileBackend(path, self.layout)

    def test_cached_index_row_still_detects_changes_to_other_files(self):
        path = self.files()
        backend = FileBackend(path, self.layout)
        self.addCleanup(backend.close)
        first = backend.rows("tb_a", {"id": "a"})
        with patch.object(Path, "open", side_effect=AssertionError("cached row was reread")):
            self.assertEqual(backend.rows("tb_a", {"id": "a"}), first)
        other = path / "tables/tb_a_config.jsonl"
        other.write_bytes(other.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "file repository changed"):
            backend.rows("tb_a", {"id": "a"})

    def test_cached_index_row_detects_changes_during_read(self):
        path = self.files()
        backend = FileBackend(path, self.layout)
        self.addCleanup(backend.close)
        backend.rows("tb_a", {"id": "a"})
        other = path / "tables/tb_a_config.jsonl"
        check = backend._check
        calls = 0

        def alter_between_checks():
            nonlocal calls
            calls += 1
            if calls == 2:
                other.write_bytes(other.read_bytes() + b"\n")
            check()

        with patch.object(backend, "_check", side_effect=alter_between_checks):
            with self.assertRaisesRegex(ValueError, "file repository changed"):
                backend.rows("tb_a", {"id": "a"})
        self.assertEqual(calls, 2)

    def test_indexed_row_cache_is_bounded_and_returns_detached_rows(self):
        self.writer.executemany(
            "INSERT INTO tb_a (id, entity_type) VALUES (?, ?)",
            [(f"id{i:04}", "A") for i in range(1025)],
        )
        path = self.files()
        backend = FileBackend(path, self.layout)
        selections = [Selection((("id", f"id{i:04}"),)) for i in range(1025)]
        groups = backend.rows_many("tb_a", selections)
        self.assertEqual(len(groups), 1025)
        self.assertEqual(len(backend.row_cache), 1024)
        groups[-1][0]["id"] = "changed"
        self.assertEqual(backend.rows("tb_a", {"id": "id1024"})[0]["id"], "id1024")
        backend.close()
        self.assertEqual(len(backend.row_cache), 0)

    def test_legacy_and_indexed_readers_reject_structural_corruption(self):
        path = self.files()
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        file = path / "tables/tb_a.jsonl"
        original = file.read_bytes()
        row = json.loads(original.splitlines()[0])
        legacy = {key: value for key, value in metadata.items() if key != "indexes"}
        variants = [
            b"[]\n",
            (json.dumps({**row, "unknown": 1}) + "\n").encode(),
            (json.dumps({**row, "id": None}) + "\n").encode(),
            (json.dumps({**row, "id": 1}) + "\n").encode(),
            (json.dumps(row) + "\n" + json.dumps(row) + "\n").encode(),
        ]
        file.write_bytes(original + b"\n")
        metadata_file.write_text(json.dumps(legacy), encoding="utf-8")
        backend = FileBackend(path, self.layout)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            backend.rows("tb_a", {"id": "a"})
        backend.close()
        for content in variants:
            with self.subTest(content=content):
                file.write_bytes(content)
                legacy["tables"]["tb_a"] = hashlib.sha256(content).hexdigest()
                metadata_file.write_text(json.dumps(legacy), encoding="utf-8")
                backend = FileBackend(path, self.layout)
                with self.assertRaisesRegex(ValueError, "tb_a.jsonl:"):
                    backend.rows("tb_a", {"id": "a"})
                backend.close()
        file.write_bytes(original)

    def test_index_offsets_reject_wrong_record_keys_and_scd_ends(self):
        self.put("A.outstanding", "7", valid_from="2026-01-01", valid_to="2026-01-03")
        path = self.files()
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        for table, position, value, message in (
            ("tb_a", 0, "wrong-id", "row does not match index"),
            ("tb_a_outstanding", -3, "2026-01-04", "interval does not match index"),
        ):
            with self.subTest(table=table):
                index = path / "indexes" / (table + ".jsonl")
                original = index.read_bytes()
                items = [json.loads(line) for line in original.splitlines()]
                items[0][position] = value
                content = "".join(json.dumps(item) + "\n" for item in items).encode()
                index.write_bytes(content)
                metadata["indexes"][table] = hashlib.sha256(content).hexdigest()
                metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
                backend = FileBackend(path, self.layout)
                with self.assertRaisesRegex(ValueError, message):
                    backend.rows(table, {"id": "a"})
                backend.close()
                index.write_bytes(original)
                metadata["indexes"][table] = hashlib.sha256(original).hexdigest()
                metadata_file.write_text(json.dumps(metadata), encoding="utf-8")

    def test_index_open_rejects_links_and_racing_repository_changes(self):
        path = self.files()
        original_symlink = Path.is_symlink

        def index_is_link(file):
            return file.parent.name == "indexes" or original_symlink(file)

        with patch.object(Path, "is_symlink", index_is_link):
            with self.assertRaisesRegex(ValueError, "index escapes root"):
                FileBackend(path, self.layout)
        stamps = FileBackend._stamps

        def racing_stamps(backend):
            current = stamps(backend)
            if any(name.startswith("index:") for name in current):
                stamp = current["repository"]
                current["repository"] = (stamp[0], stamp[1], stamp[2] + 1)
            return current

        with patch.object(FileBackend, "_stamps", racing_stamps):
            with self.assertRaisesRegex(ValueError, "changed while opening"):
                FileBackend(path, self.layout)

    def test_index_rejects_non_object_row_with_consistent_hashes(self):
        path = self.files()
        table = path / "tables/tb_a.jsonl"
        index = path / "indexes/tb_a.jsonl"
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        table.write_bytes(b"[]\n")
        index.write_text('["a",null,0,3]\n', encoding="utf-8")
        metadata["tables"]["tb_a"] = hashlib.sha256(table.read_bytes()).hexdigest()
        metadata["indexes"]["tb_a"] = hashlib.sha256(index.read_bytes()).hexdigest()
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        backend = FileBackend(path, self.layout)
        self.addCleanup(backend.close)
        with self.assertRaisesRegex(ValueError, "expected row object"):
            backend.rows("tb_a", {"id": "a"})

    def test_legacy_temporal_batch_filters_and_reuses_index(self):
        self.put("A.price", "2.5", date="2026-01-02", dim_source="X")
        self.put("A.price", "3", date="2026-01-03", dim_source="X")
        self.put("A.outstanding", "7", valid_from="2026-01-01", valid_to="2026-01-03")
        path = self.files()
        metadata_file = path / "repository.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        del metadata["indexes"]
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        backend = FileBackend(path, self.layout)
        self.addCleanup(backend.close)
        price = Selection(
            (("id", "a"), ("dim_source", "X")), "snapshot", "2026-01-02", "2026-01-02"
        )
        self.assertEqual(backend.rows_many("tb_a_price", [price], ("price",)), [[{"price": "2.5"}]])
        self.assertEqual(backend.rows_many("tb_a_price", [price], ("price",)), [[{"price": "2.5"}]])
        period = Selection((("id", "a"),), "scd", "2026-01-02", "2026-01-02")
        self.assertEqual(
            backend.rows_many("tb_a_outstanding", [period], ("outstanding",)),
            [[{"outstanding": "7"}]],
        )

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
            {**metadata, "indexes": []},
            {**metadata, "indexes": {}},
            {**metadata, "indexes": {**metadata["indexes"], "tb_a": "z" * 64}},
            {**metadata, "indexes": {**metadata["indexes"], "tb_a": 5}},
            {**metadata, "indexes": {**metadata["indexes"], "tb_a": "short"}},
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
