"""Verify calculations read physical attribute records on both backends."""

import os
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import date

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import EntityRef, Status, Success
from yambl_compiler.transactions.snapshot import SnapshotBackend


class RecordsTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    def test_records_rejects_computed_reverse_and_arguments(self):
        from tests.support import document
        from yambl_compiler.languages.yambl.semantics.validation import check

        cases = (
            (
                'entity A { computed attr x: integer { return 1 } computed attr y: bool { return self.x::records().all(r -> true) } }',
                'computed fields support',
            ),
            (
                'entity A { id id: string @reverse_relationship(B.link) attr reverse: B computed attr y: bool { return self.reverse::records().all(r -> true) } } entity B { id id: string @relationship(one, maybe) attr link: A }',
                'physically stored',
            ),
            (
                'entity A { attr x: string computed attr y: bool { return self.x::records(day=input.day).all(r -> true) } }',
                'does not accept arguments',
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertTrue(
                    any(expected in issue.message for issue in check([document(source)]))
                )

    async def test_reference_and_actual_type_failures_propagate(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """entity Base { id id: string }
entity Child inherits Base { attr label: string }
entity Holder {
    id id: string
    attr target: Base
    computed attr labels_ok: bool { return self.target.label::records().all(r -> r.value == 'x') }
}
""",
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'reference_model')
        module = load_generated(output / 'reference_model', 'reference_model')
        plan = Plan(
            'references',
            None,
            (
                Group(
                    'g',
                    (
                        Operation('insert', 'Base', ('id',), (('base',),)),
                        Operation('insert', 'Holder', ('id',), (('missing',),)),
                        Operation(
                            'insert',
                            'Holder',
                            ('id', 'target'),
                            (('ghost', 'absent'), ('base', 'base')),
                        ),
                    ),
                ),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.ReferenceData.create(config) as store:
                    self.assertEqual(store.apply(plan).status, 'committed')
                    with closing(store.read()) as reader:
                        self.assertEqual(
                            reader.get_holder('missing').value.read_labels_ok().status,
                            Status.NO_ENTRY,
                        )
                        self.assertEqual(
                            reader.get_holder('ghost').value.read_labels_ok().status,
                            Status.ENTITY_NOT_FOUND,
                        )
                        self.assertEqual(
                            reader.get_holder('base').value.read_labels_ok().status,
                            Status.NOT_DEFINED,
                        )

    async def test_temporal_and_dimensional_records(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """hierarchy Preferred { - 1 'p' }
dimension source { value source: string hierarchy priority: Hierarchy = Preferred }
dimension venue { value venue: string }
entity A {
    id id: string
    @dimension(source) @dimension(venue) @temporal(day: date) snapshot attr price: integer
    @temporal(day: date) scd attr balance: integer
    computed attr prices_ok: bool { return self.price::records().all(r -> r.source == 'p' and r.venue == 'v' and r.day == parse_date('2026-01-01') and r.value.recover(case EMPTY -> 5) > 0) }
    computed attr periods_ok: bool { return self.balance::records().all(r -> r.valid_from < r.valid_to and r.value > 0) }
    computed attr chosen_source: string { return self.price::best_available(venue='v').dimensions.source }
}
""",
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'temporal_model')
        module = load_generated(output / 'temporal_model', 'temporal_model')
        plan = Plan(
            'records',
            None,
            (
                Group(
                    'g',
                    (
                        Operation('insert', 'A', ('id',), (('a',), ('b',))),
                        Operation(
                            'replace',
                            'A.price',
                            ('source', 'venue', 'day', 'value'),
                            (('p', 'v', '2026-01-01', Symbol('EMPTY')),),
                            (Condition('id', 'a'),),
                        ),
                        Operation(
                            'replace',
                            'A.balance',
                            ('valid_from', 'valid_to', 'value'),
                            (('2026-01-01', '2026-02-01', 2),),
                            (Condition('id', 'a'),),
                        ),
                        Operation(
                            'replace',
                            'A.price',
                            ('source', 'venue', 'day', 'value'),
                            (('p', 'v', '2026-01-01', 8),),
                            (Condition('id', 'b'),),
                        ),
                    ),
                ),
            ),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.TemporalData.create(config) as store:
                    self.assertEqual(store.apply(plan).status, 'committed')
                    with closing(store.read()) as reader:
                        entity = reader.get_a('a').value
                        self.assertEqual(entity.read_prices_ok(), Success(True, 'computation'))
                        self.assertEqual(entity.read_periods_ok(), Success(True, 'computation'))
                        self.assertEqual(
                            reader.get_a('b').value.read_prices_ok(), Success(True, 'computation')
                        )
                        other = reader.get_a('b').value
                        selected = other.read_price_best_available(day=date(2026, 1, 1), venue='v')
                        self.assertEqual(
                            (
                                selected.value.value,
                                selected.value.dimensions.source,
                                selected.value.dimensions.venue,
                            ),
                            (8, 'p', 'v'),
                        )
                        self.assertEqual(other.read_chosen_source(day=date(2026, 1, 1)).value, 'p')
                    stored = store.layout.fields['A.price']
                    snapshot = store.snapshot().tables
                    price = next(row for row in snapshot[stored.table] if row['id'] == 'a')
                    price[stored.state], price[stored.value] = 0, 'bad integer'
                    with module.TemporalReader(SnapshotBackend(snapshot), store.layout) as reader:
                        records = reader._physical_records(EntityRef('A', 'a'), 'A.price')
                        self.assertIsInstance(records, Success)
                        self.assertEqual(records.value[0].value.status, Status.ERROR)
                        self.assertEqual(
                            reader.get_a('a').value.read_prices_ok().status, Status.ERROR
                        )
                    self.assertIn(
                        'closed', reader._physical_records(EntityRef('A', 'a'), 'A.price').message
                    )
                    duplicate = {
                        name: [dict(row) for row in rows] for name, rows in snapshot.items()
                    }
                    duplicate[stored.table].append(dict(price))
                    with module.TemporalReader(SnapshotBackend(duplicate), store.layout) as reader:
                        self.assertEqual(
                            reader._physical_records(EntityRef('A', 'a'), 'A.price').status,
                            Status.ERROR,
                        )
                    price['date'] = 'invalid-date'
                    with module.TemporalReader(SnapshotBackend(snapshot), store.layout) as reader:
                        self.assertEqual(
                            reader._physical_records(EntityRef('A', 'a'), 'A.price').status,
                            Status.ERROR,
                        )

    async def test_static_and_config_records_do_not_apply_effective_defaults(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """entity A {
    id id: string
    attr label: string
    config attr limit: integer = 10
    computed attr label_ok: bool { return self.label::records().all(r -> r.value == 'x') }
    computed attr no_instance_limit: bool { return self.limit::records().all(r -> r.value.recover(case NO_ENTRY -> 0) == 0) }
    computed attr config_is_empty: bool { return self.limit::records().all(r -> false) }
    computed attr effective_limit: integer { return self.limit::value() }
}
""",
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        plan = Plan(
            'records',
            None,
            (Group('g', (Operation('insert', 'A', ('id', 'label'), (('a', 'x'),)),)),),
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(plan).status, 'committed')
                    with closing(store.read()) as reader:
                        entity = reader.get_a('a').value
                        self.assertEqual(entity.label, Success('x'))
                        self.assertEqual(entity.limit, Success(10, 'model_default'))
                        self.assertEqual(entity.read_label_ok(), Success(True, 'computation'))
                        self.assertEqual(
                            entity.read_no_instance_limit(), Success(True, 'computation')
                        )
                        self.assertEqual(
                            entity.read_config_is_empty(), Success(True, 'computation')
                        )
                        self.assertEqual(entity.read_effective_limit(), Success(10, 'computation'))
                    if backend == 'sqlite':
                        script = """import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sample_model import SampleData
async def run():
    with await SampleData.open(Path(sys.argv[2])) as data:
        with data.read() as reader:
            assert reader.get_a('a').value.read_label_ok().value is True
asyncio.run(run())
assert 'yambl_compiler' not in sys.modules
"""
                        subprocess.run(
                            [sys.executable, '-I', '-B', '-c', script, str(output), str(config)],
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            check=True,
                            env={**os.environ, 'PYTHONUTF8': '1'},
                        )
                    update = Plan(
                        'update',
                        None,
                        (Group('g', (Operation('update', 'A', ('id', 'limit'), (('a', 15),)),)),),
                    )
                    self.assertEqual(store.apply(update).status, 'committed')
                    cached_reader = store.read()
                    cached = cached_reader._physical_records(EntityRef('A', 'a'), 'A.limit')
                    cached_reader.close()
                    self.assertEqual(
                        cached_reader._physical_records(EntityRef('A', 'a'), 'A.limit'), cached
                    )
                    with closing(store.read()) as reader:
                        entity = reader.get_a('a').value
                        self.assertEqual(
                            entity.read_config_is_empty(), Success(False, 'computation')
                        )
                        self.assertEqual(
                            entity.read_no_instance_limit(), Success(False, 'computation')
                        )
                        self.assertEqual(entity.read_effective_limit(), Success(15, 'computation'))
                    snapshot = store.snapshot().tables
                    stored = store.layout.fields['A.limit']
                    row = next(item for item in snapshot[stored.table] if item['id'] == 'a')
                    row[stored.state], row[stored.value] = 1, None
                    with module.SampleReader(SnapshotBackend(snapshot), store.layout) as reader:
                        records = reader._physical_records(EntityRef('A', 'a'), 'A.limit')
                        self.assertEqual(records.value[0].value.status, Status.NO_ENTRY)
                        self.assertEqual(
                            reader.get_a('a').value.limit, Success(10, 'model_default')
                        )
