"""Validate dictionary references across stored coordinates and mappings."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.layout import quote, table_name


class DictionaryValidationTests(FilesystemCase):
    def test_temporal_dimensions_and_configuration_use_candidate_dictionary(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''namespace core
decodes Sources { - 100 Known: 'Known' }
decodes OtherSources { - 100 Known: 'Known' }
entity Item {
    id key: string
    static attr source: code<Sources>
    config attr default_source: code<Sources>
    dimension vendor_source { value vendor: code<Sources> }
    @dimension(vendor_source)
    @temporal(day: date)
    snapshot attr price: decimal
    @temporal(day: date)
    scd attr rating: code<Sources>
}
entity Root { id root_id: string }
entity Left inherits Root { @overload static attr category: code<Sources> }
entity Right inherits Root { @overload static attr category: code<OtherSources> }
''',
        )
        model = compile_repository(self.root)
        out = self.root / 'generated'
        layout = generate(model, out, 'dictionary_model')
        module = load_generated(out / 'dictionary_model', 'dictionary_model')
        path = self.root / 'data.sqlite'
        with closing(sqlite3.connect(path)) as writer:
            writer.executescript((out / 'schema.sql').read_text(encoding='utf-8'))
            writer.execute(
                f'INSERT INTO {quote(table_name("core.Item"))} (id) VALUES (?)',
                ('i',),
            )
            price = layout.fields['core.Item.price']
            writer.execute(
                f'INSERT INTO {quote(price.table)} (id, dim_vendor, date, {quote(price.state)}) '
                'VALUES (?, ?, ?, ?)',
                ('i', 'Missing', '2026-01-01', 1),
            )
            rating = layout.fields['core.Item.rating']
            writer.execute(
                f'INSERT INTO {quote(rating.table)} '
                f'(id, valid_from, valid_to, {quote(rating.state)}, {quote(rating.value)}) '
                'VALUES (?, ?, ?, ?, ?)',
                ('i', '2025-01-01', '2026-01-01', 0, 'Missing'),
            )
            writer.execute(
                'INSERT INTO tb_config_group '
                '(config_group_id, target_entity_type, target_entity_type__state, priority, '
                'priority__state, when__state, then_set__state) VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('g', 'core.Item', 0, '1', 0, 0, 0),
            )
            contracts = {field.identity: field.type for field in model.fields}
            for table, identity in (
                ('tb_config_group_when', 'core.Item.source'),
                ('tb_config_group_then_set', 'core.Item.default_source'),
            ):
                envelope = json.dumps({'type': asdict(contracts[identity]), 'value': 'Missing'})
                writer.execute(
                    f'INSERT INTO {quote(table)} (config_group_id, field, value) VALUES (?, ?, ?)',
                    ('g', identity, envelope),
                )
            for group, target in (('root', 'core.Root'), ('right', 'core.Right')):
                writer.execute(
                    'INSERT INTO tb_config_group '
                    '(config_group_id, target_entity_type, target_entity_type__state, priority, '
                    'priority__state, when__state, then_set__state) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (group, target, 0, '1', 0, 0, 0),
                )
                envelope = json.dumps(
                    {'type': asdict(contracts['core.Root.category']), 'value': 'Missing'}
                )
                writer.execute(
                    'INSERT INTO tb_config_group_when (config_group_id, field, value) VALUES (?, ?, ?)',
                    (group, 'core.Root.category', envelope),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [
                    dict(row) for row in writer.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in layout.tables
            }
        files = self.root / 'files'
        files.mkdir()
        for name, text in repository_contents(layout, rows).items():
            output = files / name
            output.parent.mkdir(exist_ok=True)
            output.write_text(text, encoding='utf-8', newline='\n')
        for reader in (
            module.DictionaryReader.open(path).value,
            module.DictionaryReader.open_files(files).value,
        ):
            self.addCleanup(reader.close)
            report = reader.validate_dictionary_references({'core.Sources'})
            self.assertEqual(
                {issue.field for issue in report.issues},
                {
                    'core.Item.price',
                    'core.Item.rating',
                    'tb_config_group_when',
                    'tb_config_group_then_set',
                },
            )
            self.assertTrue(
                all('unknown code for core.Sources' in issue.message for issue in report.issues)
            )
            removed = reader.validate_dictionary_references(
                {'core.Sources'}, {'core.Sources': {'Missing'}}
            )
            self.assertIn(('root',), {issue.ids for issue in removed.issues})
            self.assertNotIn(('right',), {issue.ids for issue in removed.issues})
            full = reader.validate().value
            self.assertFalse(full.valid)
            self.assertIn('core.Item.price', {issue.field for issue in full.issues})
