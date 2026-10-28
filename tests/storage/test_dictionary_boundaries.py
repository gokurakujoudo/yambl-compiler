"""Exercise dictionary corruption diagnostics against actual reader snapshots."""

import json
import sqlite3
from dataclasses import asdict

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.records import mapped_field
from yambl_compiler.storage.validation import validate_dictionaries
from yambl_compiler.transactions.snapshot import SnapshotBackend

SOURCE = '''namespace core
decodes Sources { - 100 Known: 'Known' }
decodes OtherSources { - 100 Other: 'Other' }
entity Item {
    id key: string
    static attr source: code<Sources>
    config attr default_source: code<Sources>
    dimension prices { value vendor: code<Sources> }
    @dimension(prices) @temporal(day: date) snapshot attr price: decimal
}
entity Root { id key: string }
entity Left inherits Root { @overload static attr category: code<Sources> }
entity Right inherits Root { @overload static attr category: code<OtherSources> }
entity Third inherits Root { @overload static attr category: string }
'''


class DictionaryBoundaryTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        self.model = compile_repository(self.root)
        out = self.root / 'generated'
        self.layout = generate(self.model, out, 'boundary_model')
        self.module = load_generated(out / 'boundary_model', 'boundary_model')
        self.path = self.root / 'data.sqlite'
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.executescript((out / 'schema.sql').read_text(encoding='utf-8'))

    def reader(self):
        self.writer.commit()
        reader = self.module.BoundaryReader.open(self.path).value
        self.addCleanup(reader.close)
        return reader

    def test_invalid_dictionary_header_and_duplicate_entries_are_reported(self):
        tables = {
            table.name: [
                dict(zip((c.name for c in table.columns), row, strict=True)) for row in table.rows
            ]
            for table in self.layout.tables
        }
        headers = tables['tb_decodes']
        item = next(row for row in headers if row['decodes_id'] == 'core.Sources')
        headers.remove(item)
        report = validate_dictionaries(self.layout, SnapshotBackend(tables).rows, {'core.Sources'})
        self.assertEqual(
            [(i.field, i.message) for i in report],
            [('core.Sources', 'model dictionary missing or entries not VALUE')],
        )
        headers.append({**item, 'entries__state': 'NO_ENTRY'})
        report = validate_dictionaries(self.layout, SnapshotBackend(tables).rows, {'core.Sources'})
        self.assertEqual(report[0].message, 'model dictionary missing or entries not VALUE')
        headers[-1]['entries__state'] = 'VALUE'
        entries = tables['tb_decodes_entries']
        entries.append(next(row.copy() for row in entries if row['decodes_id'] == 'core.Sources'))
        report = validate_dictionaries(self.layout, SnapshotBackend(tables).rows, {'core.Sources'})
        self.assertEqual(
            [(i.field, i.message) for i in report], [('core.Sources', 'duplicate dictionary code')]
        )

    def test_reader_rejects_code_from_dictionary_with_invalid_state(self):
        self.writer.execute(
            "INSERT INTO tb_core_item (id,entity_type,source__state,source) VALUES ('i','core.Item','VALUE','Known')"
        )
        self.writer.execute(
            "UPDATE tb_decodes SET entries__state='NO_ENTRY' WHERE decodes_id='core.Sources'"
        )
        reader = self.reader()
        outcome = reader.get_item('i').value.read_source()
        self.assertEqual(outcome.status, self.module.Status.ERROR)
        self.assertIn('invalid dictionary state for core.Sources', outcome.message)
        report = reader.validate_dictionary_references({'core.Sources'})
        self.assertTrue(
            any(i.message == 'model dictionary missing or entries not VALUE' for i in report.issues)
        )

    def test_dictionary_scan_ignores_unowned_or_inapplicable_field_rows(self):
        self.writer.execute("PRAGMA foreign_keys=OFF")
        self.writer.execute("INSERT INTO tb_core_root (id,entity_type) VALUES ('root','core.Root')")
        self.writer.execute(
            "INSERT INTO tb_core_item_price (id,dim_vendor,date,price__state) VALUES ('ghost','Known','2025-01-01','NO_ENTRY')"
        )
        reader = self.reader()
        report = reader.validate_dictionary_references({'core.Sources'})
        self.assertTrue(report.valid, report)
        full = reader.validate()
        self.assertFalse(full.value.valid)
        self.assertTrue(any('owner' in i.message for i in full.value.issues))

    def test_invalid_mapping_envelopes_are_reported(self):
        self.writer.execute('PRAGMA ignore_check_constraints=ON')
        self.writer.execute(
            "INSERT INTO tb_config_group (config_group_id,target_entity_type,target_entity_type__state,priority,priority__state,when__state,then_set__state) VALUES ('g','core.Item','VALUE','1','VALUE','VALUE','VALUE')"
        )
        self.writer.execute(
            "INSERT INTO tb_config_group_when (config_group_id,field,value) VALUES ('g','core.Item.source','{}')"
        )
        self.writer.execute(
            "INSERT INTO tb_config_group_when (config_group_id,field,value) VALUES ('g','missing.field','{}')"
        )
        wrong = next(f.type for f in self.model.fields if f.identity == 'core.Item.price')
        envelope = json.dumps({'type': asdict(wrong), 'value': 'Known'})
        self.writer.execute(
            "INSERT INTO tb_config_group_then_set (config_group_id,field,value) VALUES ('g','core.Item.default_source',?)",
            (envelope,),
        )
        reader = self.reader()
        report = reader.validate_dictionary_references({'core.Sources'})
        self.assertEqual(
            {i.message for i in report.issues},
            {
                'invalid ConfigGroup typed value envelope',
                'ConfigGroup mapping value type disagrees with field contract',
            },
        )
        self.assertEqual({i.ids for i in report.issues}, {('g',)})

    def test_removed_code_does_not_reject_other_overload_values(self):
        self.writer.execute(
            "INSERT INTO tb_config_group "
            "(config_group_id,target_entity_type,target_entity_type__state,priority,"
            "priority__state,when__state,then_set__state) "
            "VALUES ('g','core.Root','VALUE','1','VALUE','VALUE','VALUE')"
        )
        type_ = next(f.type for f in self.model.fields if f.identity == 'core.Root.category')
        envelope = json.dumps({'type': asdict(type_), 'value': 'Present'})
        self.writer.execute(
            "INSERT INTO tb_config_group_when (config_group_id,field,value) "
            "VALUES ('g','core.Root.category',?)",
            (envelope,),
        )
        reader = self.reader()
        report = reader.validate_dictionary_references(
            {'core.Sources'}, {'core.Sources': {'Removed'}}
        )
        self.assertTrue(report.valid, report)

    def test_mapped_fields_resolve_known_and_unknown_names(self):
        stored = mapped_field(self.layout, 'core.Item.source')
        self.assertEqual((stored[0].name, stored[1]), ('source', 'core.Item'))
        derived = mapped_field(self.layout, 'core.Item.root_entity_type')
        self.assertEqual((derived[0].name, derived[1]), ('root_entity_type', 'core.Item'))
        self.assertIsNone(mapped_field(self.layout, 'core.Item.missing'))
        self.assertIsNone(mapped_field(self.layout, 'core.Unknown.field'))
