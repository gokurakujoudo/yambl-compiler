"""Exercise datetime transactions through both generated storage backends."""

import subprocess
import sys
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.results import Success
from yambl_compiler.transactions.binding import Catalog
from yambl_compiler.transactions.writing import dumps


class DatetimeTransactionTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_and_rollback(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """entity Event {
    id id: string
    static attr instant: datetime
    config attr configured: datetime
    @sparse('extra') static attr sparse_time: datetime
    @dense('wide') static attr dense_time: datetime
    @temporal(day: date) snapshot attr snapshot_time: datetime
    @temporal(day: date) scd attr scd_time: datetime
    lookup by_instant { instant = self.instant::value() }
    computed attr earlier: bool { return self.instant::value() < parse_datetime('2026-01-01T00:00:01.000001Z') }
}
""",
        )
        model = compile_repository(self.root)
        catalog = Catalog(build_layout(model))
        field, _ = catalog.field(catalog.entity('Event'), 'instant')
        self.assertEqual(
            catalog.scalar(field.type, '2026-01-01T08:00:00+08:00'), '2026-01-01T00:00:00Z'
        )
        with self.assertRaisesRegex(ValueError, 'noncanonical datetime'):
            catalog.literal(field.type, '2026-01-01T00:00:00+00:00')
        output = self.root / 'generated'
        generate(model, output, 'sample_model')
        subprocess.run(
            [
                sys.executable,
                '-I',
                '-c',
                "import sys; sys.path.insert(0, sys.argv[1]); import sample_model; assert not any(name.startswith('yambl_compiler') for name in sys.modules)",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        module = load_generated(output / 'sample_model', 'sample_model')
        first = '2026-01-01T08:00:00.123456+08:00'
        second = '2025-12-31T19:00:01-05:00'
        insert = Operation(
            'insert',
            'Event',
            ('id', 'instant', 'configured', 'sparse_time', 'dense_time'),
            (('a', first, first, first, first),),
        )
        update = Operation('update', 'Event', ('id', 'instant'), (('a', second),))
        scope = (Condition('id', 'a'),)
        snapshot = Operation(
            'replace', 'Event.snapshot_time', ('day', 'value'), (('2026-01-02', first),), scope
        )
        scd = Operation(
            'replace',
            'Event.scd_time',
            ('valid_from', 'valid_to', 'value'),
            (('2026-01-01', '2026-02-01', first),),
            scope,
        )
        initial = self.write(
            'initial.yaplan', dumps(Plan('initial', None, (Group('g', (insert, snapshot, scd)),)))
        )
        changed = self.write(
            'changed.yaplan', dumps(Plan('changed', None, (Group('g', (update,)),)))
        )
        group = Operation(
            'insert',
            'builtins.ConfigGroup',
            ('config_group_id', 'target_entity_type', 'priority', 'when', 'then_set'),
            (
                (
                    'group',
                    Symbol('Event'),
                    1,
                    ObjectValue(((Symbol('Event.instant'), second),)),
                    ObjectValue(((Symbol('Event.configured'), '2026-02-01T08:00:00+08:00'),)),
                ),
            ),
        )
        group_plan = self.write('group.yaplan', dumps(Plan('group', None, (Group('g', (group,)),))))
        expected_plan = self.write(
            'expected.yaplan',
            dumps(
                Plan(
                    'expected',
                    None,
                    (
                        Group(
                            'g',
                            (
                                Operation(
                                    'replace',
                                    'Event.snapshot_time',
                                    ('day', 'value'),
                                    (('2026-01-02', first),),
                                    scope,
                                    expect='one',
                                ),
                            ),
                        ),
                    ),
                )
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                store = await module.SampleData.create(config)
                self.addCleanup(store.close)
                self.assertEqual(store.apply(initial).status, 'committed')
                store.validate_plan_expectations(expected_plan)
                result = store.apply(changed)
                store.apply(group_plan)
                transaction = parse(result.path.read_text(encoding='utf-8'))
                self.assertEqual(
                    transaction.groups[0].changes[0].after[0].rows[0][1], '2026-01-01T00:00:01Z'
                )
                expected = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
                with closing(store.read()) as reader:
                    event = reader.get_event('a').value
                    self.assertEqual(event.instant, Success(expected))
                    self.assertEqual(event.read_earlier(), Success(True, 'computation'))
                    original = Success(datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc))
                    self.assertEqual(
                        event.configured.value, datetime(2026, 2, 1, tzinfo=timezone.utc)
                    )
                    self.assertEqual(event.sparse_time, original)
                    self.assertEqual(event.dense_time, original)
                    self.assertEqual(event.read_snapshot_time(date(2026, 1, 2)), original)
                    self.assertEqual(event.read_scd_time(date(2026, 1, 2)), original)
                    lookup = reader.prepare_event_by_instant().value
                    key = module.EventByInstantKey(
                        instant=datetime(
                            2025, 12, 31, 19, 0, 1, tzinfo=timezone(timedelta(hours=-5))
                        )
                    )
                    self.assertEqual(lookup.match_many([key]).value, (Success('a'),))
                    self.assertTrue(reader.validate().value.valid)
                reverted = store.rollback(result.path)
                reapplied = store.rollback(reverted.path)
                self.assertEqual(reapplied.status, 'committed')
                with closing(store.read()) as reader:
                    self.assertEqual(reader.get_event('a').value.instant, Success(expected))
