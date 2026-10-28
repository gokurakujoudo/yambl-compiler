"""Verify derived subtype dictionaries in generated storage and readers."""

import sqlite3
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Success


class SubtypeStorageTests(FilesystemCase):
    def test_initialization_and_computation_on_both_backends(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """
entity Root {
    id key: string
    computed attr category: code<Root::subtypes> { return Root::subtypes.Child }
    computed attr default_category: code<Root::subtypes> { return DEFAULT_CATEGORY }
}
const DEFAULT_CATEGORY: code<Root::subtypes> = Root::subtypes.Child
@decodes_rank(-10, 'child label')
entity Child inherits Root {}
entity Leaf inherits Child {}
""",
        )
        model = compile_repository(self.root)
        output = self.root / 'generated'
        layout = generate(model, output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        path = self.root / 'data.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            db.execute("INSERT INTO tb_root (id, entity_type) VALUES ('r', 'Child')")
            db.commit()
            db.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in db.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, contents in repository_contents(layout, rows).items():
            target = files / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding='utf-8', newline='\n')
        for reader in (
            module.SampleReader.open(path).value,
            module.SampleReader.open_files(files).value,
        ):
            self.addCleanup(reader.close)
            self.assertEqual(
                reader.get_root('r').value.read_default_category(),
                Success(module.RootSubtypesCode.Child, 'computation'),
            )
            self.assertEqual(
                reader.get_root('r').value.read_category(),
                Success(module.RootSubtypesCode.Child, 'computation'),
            )
        entries = [
            row for row in rows['tb_decodes_entries'] if row['decodes_id'] == 'Root::subtypes'
        ]
        self.assertEqual(
            [(row['code'], row['decode'], row['ordering']) for row in entries],
            [('Child', 'child label', '-10'), ('Leaf', 'Leaf', '200')],
        )
        self.assertTrue(any(row['decodes_id'] == 'Leaf::subtypes' for row in rows['tb_decodes']))
