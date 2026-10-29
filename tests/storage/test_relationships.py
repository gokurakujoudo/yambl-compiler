"""Exercise storage-free reverse relationships on both backends."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import date

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.records import quote
from yambl_compiler.storage.results import EntityRef, Status, Success

SOURCE = '''namespace core
dimension feed { value source: string }
entity Issuer {
    id id: string
    @reverse_relationship(Security.issuer) attr securities: collection<Security>
    @reverse_relationship(Bond.issuer) attr bonds: collection<Bond>
    @reverse_relationship(Security.lead) attr primary: Security
    @reverse_relationship(Security.contacts) attr contacted_by: collection<Security>
    @dimension(feed) @temporal(day: date) @reverse_relationship(Security.snapshot_issuer) snapshot attr snapshots: collection<Security>
    @dimension(feed) @temporal(day: date) @reverse_relationship(Security.scd_issuer) scd attr histories: collection<Security>
    @dimension(feed) @reverse_relationship(Security.assigned) attr assignments: collection<Security>
    @dimension(feed) @temporal(day: date) @reverse_relationship(Security.snapshot_single) snapshot attr snapshot_primary: Security
    @dimension(feed) @temporal(day: date) @reverse_relationship(Security.scd_single) scd attr history_primary: Security
    computed attr security_count: integer { return self.securities::value().sum_by(x -> 1) }
    lookup by_primary { primary = self.primary::value() }
    lookup by_history { primary = self.history_primary::value(day=input.day, source=input.source) }
}
entity Security {
    id id: string
    @relationship(many, one) attr issuer: Issuer
    @relationship(one, maybe) attr lead: Issuer
    @relationship(many, many) attr contacts: collection<Issuer>
    @dimension(feed) @temporal(day: date) @relationship(many, maybe) snapshot attr snapshot_issuer: Issuer
    @dimension(feed) @temporal(day: date) @relationship(many, maybe) scd attr scd_issuer: Issuer
    @dimension(feed) @relationship(many, maybe) attr assigned: Issuer
    @dimension(feed) @temporal(day: date) @relationship(one, maybe) snapshot attr snapshot_single: Issuer
    @dimension(feed) @temporal(day: date) @relationship(one, maybe) scd attr scd_single: Issuer
}
entity Bond inherits Security { @dense('shared') attr bond_note: string }
entity Other inherits Security { @dense('shared') attr other_note: string }
'''


class RelationshipRuntimeTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        self.model = compile_repository(self.root)
        output = self.root / 'generated'
        self.layout = generate(self.model, output, 'relation_model')
        self.api = load_generated(output / 'relation_model', 'relation_model')
        self.database = self.root / 'data.sqlite'
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            writer.executemany(
                'INSERT INTO tb_core_issuer (id) VALUES (?)', [('i',), ('empty',), ('lonely',)]
            )
            writer.executemany(
                'INSERT INTO tb_core_security (id,entity_type,issuer__state,issuer,lead__state,lead) VALUES (?,?,0,?,0,?)',
                [
                    ('s1', 'core.Security', 'i', 'i'),
                    ('s2', 'core.Security', 'i', 'i'),
                    ('s3', 'core.Bond', 'empty', 'i'),
                ],
            )
            contacts = self.layout.fields['core.Security.contacts']
            writer.execute(
                f'UPDATE {quote(contacts.table)} SET {quote(contacts.state)}=0 WHERE id=?', ('s1',)
            )
            writer.execute(
                f'INSERT INTO {quote(contacts.members)} (id,member_id) VALUES (?,?)', ('s1', 'i')
            )
            for name in ('snapshot_issuer', 'scd_issuer'):
                forward = self.layout.fields['core.Security.' + name]
                columns = 'date' if name.startswith('snapshot') else 'valid_from,valid_to'
                times = (
                    ('2026-01-02',) if name.startswith('snapshot') else ('2026-01-01', '2026-02-01')
                )
                marks = ','.join('?' for _ in times)
                writer.execute(
                    f'INSERT INTO {quote(forward.table)} (id,dim_source,{columns},{quote(forward.state)},{quote(forward.value)}) VALUES (?,?,{marks},0,?)',
                    ('s1', 'Feed', *times, 'i'),
                )
            forward = self.layout.fields['core.Security.scd_single']
            writer.execute(
                f'INSERT INTO {quote(forward.table)} (id,dim_source,valid_from,valid_to,{quote(forward.state)},{quote(forward.value)}) VALUES (?,?,?,?,0,?)',
                ('s1', 'Feed', '2026-01-01', '2026-02-01', 'i'),
            )
            for name, temporal in (('assigned', False), ('snapshot_single', True)):
                forward = self.layout.fields['core.Security.' + name]
                time_columns = ',date' if temporal else ''
                time_value = ('2026-01-02',) if temporal else ()
                time_mark = ',?' if temporal else ''
                writer.executemany(
                    f'INSERT INTO {quote(forward.table)} (id,dim_source{time_columns},{quote(forward.state)},{quote(forward.value)}) VALUES (?,?{time_mark},0,?)',
                    [('s1', 'Feed', *time_value, 'i'), ('s3', 'Other', *time_value, 'empty')],
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

    def test_reverse_static_and_plan_write_prohibition(self):
        self.assertEqual(self.layout.fields['core.Issuer.securities'].table, '')
        plan = Plan(
            'write',
            self.model.model_digest,
            (
                Group(
                    'changes',
                    (
                        Operation(
                            'replace',
                            'core.Issuer.securities',
                            ('value',),
                            (((),),),
                            (Condition('id', 'i'),),
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, 'field has no storage'):
            self.api.RelationData.validate_plan_syntax(plan)
        for opened in (
            self.api.RelationReader.open(self.database),
            self.api.RelationReader.open_files(self.files),
        ):
            reader = opened.value
            self.addCleanup(reader.close)
            self.assertEqual(
                reader.get_issuer('i').value.read_securities(),
                Success(
                    frozenset({EntityRef('core.Security', 's1'), EntityRef('core.Security', 's2')})
                ),
            )
            self.assertEqual(
                reader.get_issuer('empty').value.read_securities(),
                Success(frozenset({EntityRef('core.Bond', 's3')})),
            )
            self.assertEqual(
                reader.get_issuer('lonely').value.read_securities(), Success(frozenset())
            )
            self.assertEqual(
                reader.get_issuer('empty').value.read_bonds().value,
                frozenset({EntityRef('core.Bond', 's3')}),
            )
            issuer = reader.get_issuer('i').value
            self.assertEqual(issuer.read_primary().status, Status.ERROR)
            self.assertEqual(
                reader.get_issuer('empty').value.read_primary().status, Status.NO_ENTRY
            )
            self.assertEqual(
                issuer.read_contacted_by(), Success(frozenset({EntityRef('core.Security', 's1')}))
            )
            self.assertEqual(issuer.read_security_count().value, 2)
            batch = reader.read_issuer_security_count_many(
                [
                    self.api.IssuerSecurityCountRequest(id_value='i'),
                    self.api.IssuerSecurityCountRequest(id_value='empty'),
                ]
            )
            self.assertEqual([item.value for item in batch.value], [2, 1])
            self.assertEqual(
                issuer.read_snapshots(date(2026, 1, 2), source='Feed'),
                Success(frozenset({EntityRef('core.Security', 's1')})),
            )
            self.assertEqual(
                issuer.read_snapshots(date(2026, 1, 3), source='Feed'), Success(frozenset())
            )
            self.assertEqual(
                issuer.read_histories(date(2026, 1, 15), source='Feed'),
                Success(frozenset({EntityRef('core.Security', 's1')})),
            )
            self.assertEqual(
                issuer.read_histories(date(2026, 2, 1), source='Feed'), Success(frozenset())
            )
            self.assertEqual(len(issuer.snapshots.value), 1)
            self.assertEqual(len(issuer.histories.value), 1)
            self.assertEqual(
                issuer.read_histories(date(2026, 3, 1), source='Feed').value, frozenset()
            )
            self.assertEqual(len(issuer.assignments.value), 1)
            self.assertEqual(issuer.read_assignments(source='Other').value, frozenset())
            self.assertEqual(len(issuer.snapshot_primary.value), 1)
            self.assertEqual(
                issuer.read_snapshot_primary(date(2026, 1, 2), source='Feed').value,
                EntityRef('core.Security', 's1'),
            )
            self.assertEqual(
                issuer.read_snapshot_primary(date(2026, 1, 3), source='Feed').status,
                Status.NO_ENTRY,
            )
            self.assertEqual(
                issuer.read_snapshot_primary(date(2026, 1, 2), source='Other').status,
                Status.NO_ENTRY,
            )
            self.assertEqual(len(reader.get_issuer('lonely').value.snapshot_primary.value), 0)
            snapshots = issuer.read_snapshots_range(
                date(2026, 1, 1), date(2026, 1, 5), source='Feed'
            ).value
            self.assertEqual([point for point, _ in snapshots], [date(2026, 1, 2)])
            self.assertEqual(
                issuer.read_snapshots_range(date(2026, 1, 1), date(2026, 1, 1), source='Feed'),
                Success([]),
            )
            self.assertEqual(
                issuer.read_snapshot_primary_range(
                    date(2026, 1, 1), date(2026, 1, 5), source='Other'
                ),
                Success([]),
            )
            histories = issuer.read_histories_range(
                date(2026, 1, 1), date(2026, 2, 2), source='Feed'
            ).value
            self.assertEqual(
                [point for point, _ in histories], [date(2026, 1, 1), date(2026, 2, 1)]
            )
            reader.close()
            self.assertEqual(
                issuer.read_histories(date(2026, 1, 20), source='Feed').value,
                frozenset({EntityRef('core.Security', 's1')}),
            )

    def test_lookup_through_single_reverse_relationship(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute(
                "UPDATE tb_core_security SET lead__state=1,lead=NULL WHERE id IN ('s2','s3')"
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
            (self.files / path).write_text(content, encoding='utf-8', newline='\n')
        for opened in (
            self.api.RelationReader.open(self.database),
            self.api.RelationReader.open_files(self.files),
        ):
            reader = opened.value
            self.addCleanup(reader.close)
            matcher = reader.prepare_issuer_by_primary().value
            result = matcher.match_many(
                [self.api.IssuerByPrimaryKey(primary=EntityRef('core.Security', 's1'))]
            )
            self.assertEqual(result.value[0], Success('i'))
            temporal = reader.prepare_issuer_by_history(day=date(2026, 1, 2), source='Feed').value
            self.assertEqual(
                temporal.match_many(
                    [self.api.IssuerByHistoryKey(primary=EntityRef('core.Security', 's1'))]
                ).value[0],
                Success('i'),
            )

    def test_corrupt_forward_values_report_error(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute('PRAGMA ignore_check_constraints=ON')
            writer.execute("UPDATE tb_core_security SET lead=NULL WHERE id='s1'")
            stored = self.layout.fields['core.Security.snapshot_single']
            writer.execute(
                f'UPDATE {quote(stored.table)} SET {quote(stored.value)}=NULL WHERE id=?', ('s1',)
            )
            writer.commit()
        reader = self.api.RelationReader.open(self.database).value
        self.addCleanup(reader.close)
        issuer = reader.get_issuer('i').value
        self.assertEqual(issuer.read_primary().status, Status.ERROR)
        records = issuer.snapshot_primary.value
        self.assertTrue(any(record.value.status == Status.ERROR for record in records))

    def test_transactions_and_rollback_update_reverse_reads(self):
        insert = Plan(
            'insert',
            self.model.model_digest,
            (
                Group(
                    'changes',
                    (
                        Operation('insert', 'core.Issuer', ('id',), (('i',),)),
                        Operation(
                            'insert',
                            'core.Security',
                            ('id', 'issuer', 'contacts'),
                            (('s1', 'i', ('i',)),),
                        ),
                    ),
                ),
            ),
        )
        delete = Plan(
            'delete',
            self.model.model_digest,
            (
                Group(
                    'changes',
                    (Operation('delete', 'core.Security', ('id',), (('s1',),), owned_data=True),),
                ),
            ),
        )
        for backend in ('sqlite', 'files'):
            config = self.write(
                f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
            )
            with asyncio.run(self.api.RelationData.create(config)) as store:
                store.apply(insert)
                with store.read() as reader:
                    self.assertEqual(reader.validate().value.issues, ())
                    self.assertEqual(
                        reader.get_issuer('i').value.read_securities().value,
                        frozenset({EntityRef('core.Security', 's1')}),
                    )
                applied = store.apply(delete)
                with store.read() as reader:
                    self.assertEqual(
                        reader.get_issuer('i').value.read_securities().value, frozenset()
                    )
                rolled = store.rollback(applied.path)
                with store.read() as reader:
                    self.assertEqual(
                        reader.get_issuer('i').value.read_securities().value,
                        frozenset({EntityRef('core.Security', 's1')}),
                    )
                store.rollback(rolled.path)
                with store.read() as reader:
                    self.assertEqual(
                        reader.get_issuer('i').value.read_securities().value, frozenset()
                    )

    def test_deletion_does_not_discard_corrupt_sibling_data(self):
        config = self.write('corrupt.lclcfg', "backend: 'sqlite'\npath: 'corrupt.data'\n")
        insert = Plan(
            'insert',
            self.model.model_digest,
            (
                Group(
                    'changes',
                    (
                        Operation('insert', 'core.Issuer', ('id',), (('i',),)),
                        Operation(
                            'insert',
                            'core.Bond',
                            ('id', 'issuer', 'contacts', 'bond_note'),
                            (('s', 'i', ('i',), 'ok'),),
                        ),
                    ),
                ),
            ),
        )
        delete = Plan(
            'delete',
            self.model.model_digest,
            (
                Group(
                    'changes',
                    (Operation('delete', 'core.Bond', ('id',), (('s',),), owned_data=True),),
                ),
            ),
        )
        with asyncio.run(self.api.RelationData.create(config)) as store:
            store.apply(insert)
        sibling = self.layout.fields['core.Security.other_note']
        with closing(sqlite3.connect(self.root / 'corrupt.data')) as writer:
            writer.execute(
                f'UPDATE {quote(sibling.table)} SET {quote(sibling.state)}=0,{quote(sibling.value)}=? WHERE id=?',
                ('keep', 's'),
            )
            writer.commit()
        with asyncio.run(self.api.RelationData.open(config)) as store:
            before = store.snapshot().tables
            with self.assertRaisesRegex(ValueError, 'missing owner'):
                store.apply(delete)
            self.assertEqual(store.snapshot().tables, before)
