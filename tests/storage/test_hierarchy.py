"""Exercise named and dictionary-backed hierarchies through both storage backends."""

import asyncio
import inspect
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date
from decimal import Decimal

from tests.support import FilesystemCase, document, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol
from yambl_compiler.languages.yambl.semantics.validation import analyze
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.records import quote, table_name
from yambl_compiler.storage.results import Status, Success

SOURCE = """namespace core
decodes Sources { - 1 Wind: 'Wind' - 2 Manual: 'Manual' }
@flags decodes Flags { - 1 First: 'First' }
hierarchy H { - 10 Sources.Wind - 20 'Manual' }
hierarchy Empty {}
dimension shared { value source: string hierarchy order: Hierarchy = H }
entity E {
    id id: string
    attr dictionary: Decodes
    @sparse('extra') attr sparse_dictionary: Decodes
    attr preferred_source: string
    config attr limit: integer = 5
    @temporal(day: date) scd attr dictionary_history: Decodes
    dimension local {
        value source: string
        hierarchy order: Hierarchy = self.dictionary::value()::hierarchy().recover(case NO_ENTRY -> H)
    }
    dimension historical {
        value source: string
        hierarchy order: Hierarchy = self.dictionary_history::value()::hierarchy().recover(case NO_ENTRY -> H)
    }
    dimension computed_source {
        value source: string
        hierarchy order: Hierarchy = self.selected::value()::hierarchy()
    }
    dimension with_argument {
        value source: string
        hierarchy order: Hierarchy = self.dictionary_history::value(day=input.query)::hierarchy()
    }
    computed attr selected: Decodes { return self.dictionary_history::value(day=input.query) }
    @dimension(local) @temporal(day: date) snapshot attr price: decimal
    @dimension(shared) @temporal(day: date) snapshot attr shared_price: decimal
    @dimension(historical) @temporal(day: date) snapshot attr historical_price: decimal
    @dimension(historical) attr static_price: decimal
    @dimension(computed_source) attr computed_price: decimal
    @dimension(with_argument) attr argument_price: decimal
    computed attr best: decimal { return self.price::best_available(day=input.day).value }
    computed attr shared_best: decimal { return self.shared_price::best_available(day=input.day).value }
    computed attr historical_best: decimal { return self.historical_price::best_available(day=input.as_of).value }
    computed attr static_view: Hierarchy { return Sources::hierarchy() }
    computed attr from_static: decimal { return self.price::best_available(day=input.day, order=Sources::hierarchy()).value }
    computed attr static_best: decimal { return self.static_price::best_available().value }
    computed attr computed_best: decimal { return self.computed_price::best_available().value }
    computed attr argument_best: decimal { return self.argument_price::best_available().value }
    computed attr explicit_source: decimal { return self.price::best_available(day=input.day, source=self.preferred_source::value()).value }
    computed attr chosen_source: string { return self.price::best_available(day=input.day).dimensions.source }
}
entity Holder {
    id id: string
    attr target: E
    computed attr best: decimal { return self.target::value().price::best_available(day=input.day).value }
    computed attr historical_best: decimal { return self.target::value().historical_price::best_available(day=input.as_of).value }
}
"""


class HierarchyRuntimeTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        self.model = compile_repository(self.root)
        output = self.root / 'generated'
        self.layout = generate(self.model, output, 'sample_model')
        self.api = load_generated(output / 'sample_model', 'sample_model')
        self.database = self.root / 'data.sqlite'
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            writer.executemany(
                'INSERT INTO tb_core_e (id,dictionary__state,dictionary) VALUES (?, ?, ?)',
                [
                    ('fallback', 1, None),
                    ('declared', 0, 'core.Sources'),
                    ('empty', 0, 'empty'),
                    ('empty_normal', 0, 'empty_normal'),
                    ('missing', 0, 'missing'),
                    ('duplicate', 0, 'duplicate'),
                    ('flags', 0, 'core.Flags'),
                ],
            )
            writer.executemany(
                'INSERT INTO tb_core_holder (id,target__state,target) VALUES (?, 0, ?)',
                [('fallback', 'fallback'), ('declared', 'declared'), ('missing', 'missing')],
            )
            writer.executemany(
                'INSERT INTO tb_decodes (decodes_id,entries__state) VALUES (?, ?)',
                [('empty', 2), ('empty_normal', 0), ('duplicate', 0)],
            )
            writer.executemany(
                'INSERT INTO tb_decodes_entries (decodes_id,code,decode,ordering) VALUES (?, ?, ?, ?)',
                [('duplicate', 'One', 'One', '1'), ('duplicate', 'Two', 'Two', '1')],
            )
            dictionary_history = self.layout.fields['core.E.dictionary_history']
            writer.executemany(
                f'INSERT INTO {quote(dictionary_history.table)} (id,valid_from,valid_to,{quote(dictionary_history.state)},{quote(dictionary_history.value)}) VALUES (?, ?, ?, ?, ?)',
                [
                    ('declared', '2026-01-01', '2027-01-01', 0, 'core.Sources'),
                    ('missing', '2026-01-01', '2027-01-01', 0, 'missing'),
                ],
            )
            for identity in (
                'fallback',
                'declared',
                'empty',
                'empty_normal',
                'missing',
                'duplicate',
                'flags',
            ):
                for name in ('price', 'shared_price', 'historical_price'):
                    field = self.layout.fields['core.E.' + name]
                    writer.execute(
                        f'INSERT INTO {quote(field.table)} (id,dim_source,date,{quote(field.state)},{quote(field.value)}) VALUES (?,?,?,?,?)',
                        (identity, 'Wind', '2026-01-01', 0, '7.5'),
                    )
                field = self.layout.fields['core.E.static_price']
                computed_field = self.layout.fields['core.E.computed_price']
                argument_field = self.layout.fields['core.E.argument_price']
                writer.execute(
                    f'INSERT INTO {quote(field.table)} (id,dim_source,{quote(field.state)},{quote(field.value)},{quote(computed_field.state)},{quote(computed_field.value)},{quote(argument_field.state)},{quote(argument_field.value)}) VALUES (?,?,?,?,?,?,?,?)',
                    (identity, 'Wind', 0, '7.5', 0, '7.5', 0, '7.5'),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            tables = {
                table.name: [
                    dict(row) for row in writer.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in self.layout.tables
            }
        self.files = self.root / 'files'
        for path, content in repository_contents(self.layout, tables).items():
            target = self.files / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')

    def test_declared_and_dynamic_hierarchy_defaults(self):
        for opened in (
            self.api.SampleReader.open(self.database),
            self.api.SampleReader.open_files(self.files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            self.addCleanup(reader.close)
            selected = reader.get_e('fallback').value
            result = selected.read_price_best_available(day=date(2026, 1, 1))
            self.assertEqual(result.value.value, Decimal('7.5'))
            self.assertEqual(result.value.dimensions.source, 'Wind')
            self.assertEqual(selected.read_chosen_source(day=date(2026, 1, 1)).value, 'Wind')
            explicit = selected.read_price_best_available(day=date(2026, 1, 1), source='Wind')
            self.assertEqual(explicit.value, result.value)
            declared = reader.get_e('declared').value
            self.assertIn(
                'query', inspect.signature(declared.read_argument_price_best_available).parameters
            )
            dependent = declared.read_argument_price_best_available(query=date(2026, 1, 1))
            self.assertEqual(dependent.value.value, Decimal('7.5'))
            self.assertEqual(dependent.value.dimensions.source, 'Wind')
            self.assertEqual(
                declared.read_argument_price_best_available(source='Wind').value.value,
                Decimal('7.5'),
            )
            config = selected.read_limit_best_available()
            self.assertEqual(config.value.value, 5)
            self.assertEqual(config.origin, 'model_default')
            self.assertEqual(vars(config.value.dimensions), {})
            self.assertEqual(
                selected.read_price_best_available(day=date(2025, 1, 1)).status, Status.NO_ENTRY
            )
            self.assertEqual(
                reader._computing.available.hierarchy(['core.Empty'])['core.Empty'], Success(())
            )
            for identity in ('fallback', 'declared'):
                entity = reader.get_e(identity).value
                self.assertEqual(entity.read_best(day=date(2026, 1, 1)).value, Decimal('7.5'))
                self.assertEqual(
                    entity.read_shared_best(day=date(2026, 1, 1)).value, Decimal('7.5')
                )
                self.assertEqual(entity.read_static_view().value.dictionary, 'core.Sources')
                self.assertEqual(
                    entity.read_from_static(day=date(2026, 1, 1)).value, Decimal('7.5')
                )
                self.assertEqual(
                    reader.get_holder(identity).value.read_best(day=date(2026, 1, 1)).value,
                    Decimal('7.5'),
                )
                self.assertEqual(
                    entity.read_historical_best(as_of=date(2026, 1, 1)).value, Decimal('7.5')
                )
                self.assertEqual(
                    reader.get_holder(identity)
                    .value.read_historical_best(as_of=date(2026, 1, 1))
                    .value,
                    Decimal('7.5'),
                )
            for identity, status in (
                ('empty', Status.EMPTY),
                ('empty_normal', Status.NO_ENTRY),
                ('missing', Status.ENTITY_NOT_FOUND),
                ('duplicate', Status.ERROR),
                ('flags', Status.ERROR),
            ):
                result = reader.get_e(identity).value.read_best(day=date(2026, 1, 1))
                self.assertEqual(result.status, status)
            self.assertEqual(
                reader.get_holder('missing').value.read_best(day=date(2026, 1, 1)).status,
                Status.ENTITY_NOT_FOUND,
            )
            self.assertEqual(
                reader.get_holder('missing')
                .value.read_historical_best(as_of=date(2026, 1, 1))
                .status,
                Status.ENTITY_NOT_FOUND,
            )
            method = reader.get_holder('missing').value.read_historical_best
            self.assertNotIn('day', inspect.signature(method).parameters)
            self.assertEqual(method(as_of=date(2026, 1, 1), source='Wind').value, Decimal('7.5'))
            static = reader.get_e('missing').value.read_static_best
            self.assertEqual(static(order='core.H').value, Decimal('7.5'))
            self.assertEqual(static(source='Wind').value, Decimal('7.5'))
            self.assertEqual(static().status, Status.ERROR)
            computed = reader.get_e('declared').value.read_computed_best
            self.assertEqual(computed(query=date(2026, 1, 1)).value, Decimal('7.5'))
            self.assertEqual(computed(order='core.H').value, Decimal('7.5'))
            self.assertEqual(computed().status, Status.ERROR)
            self.assertEqual(
                reader.get_e('declared').value.read_argument_best(query=date(2026, 1, 1)).value,
                Decimal('7.5'),
            )
            self.assertEqual(
                reader.get_e('missing').value.read_explicit_source(day=date(2026, 1, 1)).status,
                Status.NO_ENTRY,
            )
            reader.close()

    def test_generated_package_runs_without_compiler(self):
        script = '''
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import sample_model as model
assert not any(name == 'yambl_compiler' or name.startswith('yambl_compiler.') for name in sys.modules)
for opened in (model.SampleReader.open(Path(sys.argv[2])), model.SampleReader.open_files(Path(sys.argv[3]))):
    assert isinstance(opened, model.Success), opened
    reader = opened.value
    assert reader.get_holder('declared').value.read_historical_best(as_of=date(2026, 1, 1)).value == Decimal('7.5')
    assert reader.close() == model.Success(None)
'''
        result = subprocess.run(
            [
                sys.executable,
                '-I',
                '-c',
                script,
                str(self.root / 'generated'),
                str(self.database),
                str(self.files),
            ],
            capture_output=True,
            encoding='utf-8',
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_validation_detects_external_changes_to_model_hierarchies(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("DELETE FROM tb_hierarchy WHERE hierarchy_id='core.Empty'")
            writer.execute(
                "UPDATE tb_hierarchy_entries SET entry_value='Changed' WHERE hierarchy_id='core.H' AND ordering='10'"
            )
            writer.commit()
            writer.row_factory = sqlite3.Row
            tables = {
                table.name: [
                    dict(row) for row in writer.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in self.layout.tables
            }
        for path, content in repository_contents(self.layout, tables).items():
            target = self.files / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
        for opened in (
            self.api.SampleReader.open(self.database),
            self.api.SampleReader.open_files(self.files),
        ):
            reader = opened.value
            self.addCleanup(reader.close)
            messages = [issue.message for issue in reader.validate().value.issues]
            self.assertIn('model hierarchy missing or entries not VALUE', messages)
            self.assertIn('model hierarchy entry changed: 10', messages)

    def test_model_entries_are_protected_and_extensions_roll_back(self):
        def entry(order, value):
            return ObjectValue(((Symbol('ordering'), order), (Symbol('entry_value'), value)))

        def plan(name, entries):
            operation = Operation(
                'replace',
                'builtins.Hierarchy.hierarchy_entries',
                ('value',),
                ((entries,),),
                (Condition('hierarchy_id', 'core.H'),),
            )
            return Plan(name, self.model.model_digest, (Group('change', (operation,)),))

        original = (entry(10, 'Wind'), entry(20, 'Manual'))
        extended = (*original, entry(30, 'Extra'))
        delete = Plan(
            'delete',
            self.model.model_digest,
            (
                Group(
                    'change',
                    (
                        Operation(
                            'delete',
                            'builtins.Hierarchy',
                            ('hierarchy_id',),
                            (('core.Empty',),),
                            owned_data=True,
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, 'model hierarchy cannot be deleted'):
            self.api.SampleData.validate_plan_syntax(delete)
        with self.assertRaisesRegex(ValueError, 'model hierarchy entries are immutable'):
            self.api.SampleData.validate_plan_syntax(plan('invalid', (entry(10, 'Changed'),)))
        self.api.SampleData.validate_plan_syntax(plan('extend', extended))
        dynamic = Plan(
            'dynamic',
            self.model.model_digest,
            (
                Group(
                    'change',
                    (
                        Operation(
                            'replace',
                            'builtins.Hierarchy.hierarchy_entries',
                            ('value',),
                            (((entry(1, 'Dynamic'),),),),
                            (Condition('hierarchy_id', 'dynamic'),),
                        ),
                    ),
                ),
            ),
        )
        self.api.SampleData.validate_plan_syntax(dynamic)
        empty_state = Plan(
            'empty_state',
            self.model.model_digest,
            (
                Group(
                    'change',
                    (
                        Operation(
                            'replace',
                            'builtins.Hierarchy.hierarchy_entries',
                            ('value',),
                            ((Symbol('EMPTY'),),),
                            (Condition('hierarchy_id', 'core.Empty'),),
                        ),
                    ),
                ),
            ),
        )
        self.api.SampleData.validate_plan_syntax(empty_state)
        for backend in ('sqlite', 'files'):
            config = self.write(
                f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
            )
            with asyncio.run(self.api.SampleData.create(config)) as store:
                before = store.snapshot().tables
                with self.assertRaisesRegex(ValueError, 'candidate hierarchy validation failed'):
                    store.apply(empty_state)
                with self.assertRaisesRegex(ValueError, 'model hierarchy entries are immutable'):
                    store.apply(plan('invalid', (entry(10, 'Changed'),)))
                self.assertEqual(store.snapshot().tables, before)
                applied = store.apply(plan('extend', extended))
                self.assertEqual(applied.status, 'committed')
                after = store.snapshot().tables
                self.assertNotEqual(after, before)
                rolled = store.rollback(applied.path)
                self.assertEqual(store.snapshot().tables, before)
                store.rollback(rolled.path)
                self.assertEqual(store.snapshot().tables, after)

    def test_dictionary_view_uses_each_reader_snapshot(self):
        def entry(code, order):
            return ObjectValue(
                ((Symbol('code'), code), (Symbol('decode'), code), (Symbol('ordering'), order))
            )

        initial = Plan(
            'initial',
            self.model.model_digest,
            (
                Group(
                    'change',
                    (
                        Operation(
                            'insert', 'core.E', ('id', 'dictionary'), (('one', 'core.Sources'),)
                        ),
                        Operation(
                            'replace',
                            'core.E.price',
                            ('day', 'source', 'value'),
                            (
                                ('2026-01-01', 'Wind', Decimal('7.5')),
                                ('2026-01-01', 'New', Decimal('9')),
                            ),
                            (Condition('id', 'one'),),
                        ),
                    ),
                ),
            ),
        )
        extended = Plan(
            'extend',
            self.model.model_digest,
            (
                Group(
                    'change',
                    (
                        Operation(
                            'replace',
                            'builtins.Decodes.entries',
                            ('value',),
                            (((entry('Wind', 1), entry('Manual', 2), entry('New', 0)),),),
                            (Condition('decodes_id', 'core.Sources'),),
                        ),
                    ),
                ),
            ),
        )
        for backend in ('sqlite', 'files'):
            config = self.write(
                f'{backend}-snapshot.lclcfg',
                f"backend: '{backend}'\npath: '{backend}-snapshot.data'\n",
            )
            with asyncio.run(self.api.SampleData.create(config)) as store:
                store.apply(initial)
                with store.read() as old:
                    view = old.get_e('one').value.read_static_view().value
                    self.assertEqual(
                        old.get_e('one').value.read_best(day=date(2026, 1, 1)).value, Decimal('7.5')
                    )
                    store.apply(extended)
                    self.assertEqual(
                        old.get_e('one').value.read_best(day=date(2026, 1, 1)).value, Decimal('7.5')
                    )
                    with store.read() as new:
                        self.assertEqual(
                            new.get_e('one').value.read_best(day=date(2026, 1, 1)).value,
                            Decimal('9'),
                        )
                        self.assertEqual(
                            new.get_e('one')
                            .value.read_best(day=date(2026, 1, 1), order=view)
                            .status,
                            Status.ERROR,
                        )

    def test_identical_defaults_keep_their_declaration_scopes(self):
        from pathlib import Path

        template = (
            "namespace {namespace} hierarchy H {{ - 1 '{value}' }} "
            "dimension d {{ value source: string hierarchy h: Hierarchy = H }} "
            "entity {entity} {{ id id: string @dimension(d) attr price: decimal "
            "computed attr result: decimal {{ return self.price::best_available().value }} }}"
        )
        sources = [
            template.format(namespace='one', value='X', entity='A'),
            template.format(namespace='two', value='Y', entity='B'),
        ]
        analysis = analyze([document(source, f'{i}.yambl') for i, source in enumerate(sources)])
        self.assertEqual(analysis.diagnostics, ())
        model = Lowering(analysis, Path('.'), '', 'test').model()
        output = self.root / 'cross'
        layout = generate(model, output, 'cross_model')
        api = load_generated(output / 'cross_model', 'cross_model')
        database = self.root / 'cross.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            for owner, source, value in (('one.A', 'X', '1'), ('two.B', 'Y', '2')):
                field = layout.fields[owner + '.price']
                writer.execute(f'INSERT INTO {quote(table_name(owner))} (id) VALUES (?)', ('id',))
                writer.execute(
                    f'INSERT INTO {quote(field.table)} (id,dim_source,{quote(field.state)},{quote(field.value)}) VALUES (?,?,?,?)',
                    ('id', source, 0, value),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            tables = {
                table.name: [
                    dict(row) for row in writer.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in layout.tables
            }
        files = self.root / 'cross-files'
        for path, content in repository_contents(layout, tables).items():
            target = files / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
        for opened in (api.CrossReader.open(database), api.CrossReader.open_files(files)):
            reader = opened.value
            self.addCleanup(reader.close)
            self.assertEqual(reader.get_a('id').value.read_result().value, Decimal('1'))
            self.assertEqual(reader.get_b('id').value.read_result().value, Decimal('2'))
            reader.close()

    def test_invalid_dictionary_code_does_not_fail_other_batch_items(self):
        self.write(
            'model.yambl',
            '''namespace core
decodes Sources { - 1 Wind: 'Wind' }
entity E {
    id id: string
    attr dictionary: Decodes
    dimension d { value source: code<Sources> hierarchy order: Hierarchy = self.dictionary::value()::hierarchy() }
    @dimension(d) attr price: decimal
    computed attr best: decimal { return self.price::best_available().value }
}
''',
        )
        model = compile_repository(self.root)
        output = self.root / 'batch-generated'
        layout = generate(model, output, 'batch_model')
        api = load_generated(output / 'batch_model', 'batch_model')
        database = self.root / 'batch.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            writer.executemany(
                'INSERT INTO tb_core_e (id,dictionary__state,dictionary) VALUES (?,0,?)',
                [('good', 'core.Sources'), ('bad', 'other')],
            )
            writer.execute("INSERT INTO tb_decodes (decodes_id,entries__state) VALUES ('other',0)")
            writer.execute(
                "INSERT INTO tb_decodes_entries (decodes_id,code,decode,ordering) VALUES ('other','Bogus','Bogus','1')"
            )
            stored = layout.fields['core.E.price']
            writer.execute(
                f'INSERT INTO {quote(stored.table)} (id,dim_source,{quote(stored.state)},{quote(stored.value)}) VALUES (?,?,?,?)',
                ('good', 'Wind', 0, '7.5'),
            )
            writer.commit()
            writer.row_factory = sqlite3.Row
            tables = {
                table.name: [
                    dict(row) for row in writer.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in layout.tables
            }
        files = self.root / 'batch-files'
        for path, content in repository_contents(layout, tables).items():
            target = files / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
        for opened in (api.BatchReader.open(database), api.BatchReader.open_files(files)):
            reader = opened.value
            self.addCleanup(reader.close)
            result = reader.read_e_best_many(
                [api.EBestRequest(id_value='good'), api.EBestRequest(id_value='bad')]
            )
            self.assertIsInstance(result, Success)
            self.assertEqual(result.value[0].value, Decimal('7.5'))
            self.assertEqual(result.value[1].status, Status.ERROR)
