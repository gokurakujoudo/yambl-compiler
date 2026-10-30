"""Exercise actual entity types with and without physical discriminator columns."""

import asyncio
import json
import sqlite3
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success
from yambl_compiler.transactions.validation import PlanValidator


class AbstractEntityTests(FilesystemCase):
    def test_abstract_contract_and_creation_boundary(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''abstract entity Root {
    id id: string
    abstract computed attr number: integer
    computed attr doubled: integer { return self.number::value() * 2 }
}
entity One inherits Root {
    @override computed attr number: integer { return 1 }
}
entity Two inherits Root {
    @override computed attr number: integer { return 2 }
}
''',
        )
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'abstract_model')
        model = load_generated(output / 'abstract_model', 'abstract_model')
        invalid = Plan(
            'invalid', None, (Group('g', (Operation('insert', 'Root', ('id',), (('r',),)),)),)
        )
        with self.assertRaisesRegex(ValueError, 'abstract entity'):
            PlanValidator(layout).validate(invalid)
        valid = Plan(
            'valid',
            None,
            (
                Group(
                    'g',
                    (
                        Operation('insert', 'One', ('id',), (('one',),)),
                        Operation('insert', 'Two', ('id',), (('two',),)),
                    ),
                ),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    backend + '.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with asyncio.run(model.AbstractData.create(config)) as data:
                    with self.assertRaisesRegex(ValueError, 'abstract entity'):
                        data.apply(invalid)
                    with data.read() as reader:
                        self.assertEqual(reader.get_root('r').status, Status.NO_ENTRY)
                    self.assertEqual(data.apply(valid).status, 'committed')
                    with data.read() as reader:
                        self.assertEqual(
                            reader.get_root('one').value.read_doubled(), Success(2, 'computation')
                        )
                        self.assertEqual(
                            reader.get_root('two').value.read_doubled(), Success(4, 'computation')
                        )
                        self.assertTrue(reader.validate().value.valid)
                if backend == 'sqlite':
                    with closing(sqlite3.connect(self.root / 'sqlite.data')) as db, db:
                        db.execute("UPDATE tb_root SET entity_type = 'Root' WHERE id = 'one'")
                    reader = model.AbstractReader.open_sqlite(self.root / 'sqlite.data').value
                    try:
                        self.assertIn(
                            'abstract entity instance: Root', str(reader.validate().value.issues)
                        )
                    finally:
                        reader.close()


class DiscriminatorTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            '''
final entity Solo {
    id id: string
    attr name: string
    lookup by_name { name = self.name::value() }
}
entity Root {
    id id: string
    attr name: string
    lookup by_name { name = self.name::value() }
}
final entity Child inherits Root {}
''',
        )
        self.output = self.root / 'generated'
        self.layout = generate(compile_repository(self.root), self.output, 'sample_model')
        self.module = load_generated(self.output / 'sample_model', 'sample_model')
        self.plan = Plan(
            'create',
            self.layout.model.model_digest,
            (
                Group(
                    'items',
                    (
                        Operation('insert', 'Solo', ('id', 'name'), (('s', 'single'),)),
                        Operation('insert', 'Root', ('id', 'name'), (('r', 'root'),)),
                        Operation('insert', 'Child', ('id', 'name'), (('c', 'child'),)),
                    ),
                ),
            ),
        )

    def test_columns_reads_lookup_and_rollback_on_both_backends(self):
        columns = {
            table.name: {column.name for column in table.columns} for table in self.layout.tables
        }
        self.assertNotIn('entity_type', columns['tb_solo'])
        self.assertIn('entity_type', columns['tb_root'])
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    backend + '.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with asyncio.run(self.module.SampleData.create(config)) as data:
                    applied = data.apply(self.plan)
                    with data.read() as reader:
                        self.assertEqual(reader.get_solo('s').value.entity_type, 'Solo')
                        self.assertEqual(reader.get_solo('s').value.read_name(), Success('single'))
                        self.assertEqual(reader.get_root('r').value.entity_type, 'Root')
                        self.assertEqual(reader.get_root('c').value.entity_type, 'Child')
                        self.assertEqual(reader.get_child('c').value.root_entity_type, 'Root')
                        self.assertEqual(
                            reader.prepare_solo_by_name().value.match_many(
                                [self.module.SoloByNameKey(name='single')]
                            ),
                            Success((Success('s'),)),
                        )
                        self.assertEqual(
                            reader.prepare_child_by_name().value.match_many(
                                [self.module.RootByNameKey(name='child')]
                            ),
                            Success((Success('c'),)),
                        )
                        self.assertTrue(reader.validate().value.valid)
                    reverted = data.rollback(applied.path)
                    with data.read() as reader:
                        self.assertEqual(reader.get_solo('s').status, Status.NO_ENTRY)
                        self.assertEqual(reader.get_root('c').status, Status.NO_ENTRY)
                    data.rollback(reverted.path)
                    with data.read() as reader:
                        self.assertEqual(reader.get_solo('s').value.entity_type, 'Solo')
                        self.assertEqual(reader.get_root('c').value.entity_type, 'Child')

    def test_old_storage_revisions_are_rejected(self):
        sqlite = self.root / 'old.sqlite'
        with closing(sqlite3.connect(sqlite)) as db:
            db.executescript((self.output / 'schema.sql').read_text(encoding='utf-8'))
            db.execute("UPDATE tb_yambl_metadata SET value = '8' WHERE key = 'layout_revision'")
            db.commit()
        result = self.module.SampleReader.open_sqlite(sqlite)
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn('revision', result.message)

        repository = self.output / 'repository'
        metadata_path = repository / 'repository.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        for key, old in (('layout_revision', 8), ('format_revision', 2)):
            outdated = {**metadata, key: old}
            metadata_path.write_text(json.dumps(outdated), encoding='utf-8')
            result = self.module.SampleReader.open_files(repository)
            self.assertEqual(result.status, Status.ERROR)
            self.assertIn('revision', result.message)
