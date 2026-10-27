"""Verify candidate validation remains mandatory at transaction boundaries."""

import asyncio
from unittest.mock import patch

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.languages.yatran.model import Transaction
from yambl_compiler.languages.yatran.writing import seal
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution, rollback


class CandidateValidationTests(CatalogCase):
    """Keep failed candidates away from both persistent backends."""

    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.module = load_generated(output / 'sample_model', 'sample_model')
        self.plan = Plan(
            'insert_one',
            self.catalog.layout.model.model_digest,
            (Group('create', (Operation('insert', 'A', ('key',), (('a',),)),)),),
        )

    def store(self, backend):
        config = self.write(backend + '.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n")
        return asyncio.run(self.module.SampleData.create(config))

    def test_each_persistent_operation_validates_once_and_changes_expected_data(self):
        candidate_type = self.module.SampleData.commit.__globals__['IncrementalData']
        original = candidate_type.validate
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend), self.store(backend) as data:
                calls = []

                def validate(candidate):
                    calls.append(candidate)
                    return original(candidate)

                with patch.object(candidate_type, 'validate', validate):
                    report = data.apply(self.plan)
                self.assertEqual(len(calls), 1)
                with data.read() as reader:
                    self.assertEqual(reader.get_a('a').value.id, 'a')
                calls.clear()
                with patch.object(candidate_type, 'validate', validate):
                    reversed_report = data.rollback(report.path)
                self.assertEqual(len(calls), 1)
                self.assertEqual(reversed_report.status, 'committed')
                with data.read() as reader:
                    self.assertEqual(reader.get_a('a').status, Status.NO_ENTRY)

    def test_failed_final_validation_preserves_existing_data_and_transaction_history(self):
        candidate_type = self.module.SampleData.commit.__globals__['IncrementalData']
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend), self.store(backend) as data:
                empty = data.snapshot().tables
                before = set(data.transaction_directory().glob('*.yatran'))
                with patch.object(
                    candidate_type, 'validate', side_effect=ValueError('invalid candidate')
                ):
                    with self.assertRaisesRegex(ValueError, 'invalid candidate'):
                        data.apply(self.plan)
                self.assertEqual(data.snapshot().tables, empty)
                self.assertEqual(set(data.transaction_directory().glob('*.yatran')), before)
                report = data.apply(self.plan)
                seeded = data.snapshot().tables
                transactions = set(data.transaction_directory().glob('*.yatran'))
                with patch.object(
                    candidate_type, 'validate', side_effect=ValueError('invalid candidate')
                ):
                    with self.assertRaisesRegex(ValueError, 'invalid candidate'):
                        data.rollback(report.path)
                self.assertEqual(data.snapshot().tables, seeded)
                self.assertEqual(set(data.transaction_directory().glob('*.yatran')), transactions)
                with data.read() as reader:
                    self.assertEqual(reader.get_a('a').value.id, 'a')

    def test_failed_business_validation_preserves_data_and_transaction_history(self):
        failure = ValueError('candidate data validation failed: business check failed')
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend), self.store(backend) as data:
                empty = data.snapshot().tables
                before = set(data.transaction_directory().glob('*.yatran'))
                with patch.object(
                    self.module.SampleReader, 'validate_candidate', side_effect=failure
                ):
                    with self.assertRaisesRegex(ValueError, 'candidate data validation failed'):
                        data.apply(self.plan)
                self.assertEqual(data.snapshot().tables, empty)
                self.assertEqual(set(data.transaction_directory().glob('*.yatran')), before)
                report = data.apply(self.plan)
                seeded = data.snapshot().tables
                transactions = set(data.transaction_directory().glob('*.yatran'))
                with patch.object(
                    self.module.SampleReader, 'validate_candidate', side_effect=failure
                ):
                    with self.assertRaisesRegex(ValueError, 'candidate data validation failed'):
                        data.rollback(report.path)
                self.assertEqual(data.snapshot().tables, seeded)
                self.assertEqual(set(data.transaction_directory().glob('*.yatran')), transactions)

    def test_direct_execution_still_requires_physical_validation(self):
        tables = {
            table.name: [
                dict(zip((column.name for column in table.columns), row, strict=True))
                for row in table.rows
            ]
            for table in self.catalog.layout.tables
        }
        candidate = Data(self.catalog.layout, tables)
        transaction = seal(
            Transaction(
                '8c24b950-0d0f-414e-a873-617665905d03', '2026-10-27T00:00:00Z', self.plan, ()
            )
        )
        with patch.object(Data, 'validate', side_effect=ValueError('invalid candidate')):
            with self.assertRaisesRegex(ValueError, 'invalid candidate'):
                Execution(candidate).apply(Plan('empty', self.plan.model, ()))
            with self.assertRaisesRegex(ValueError, 'invalid candidate'):
                rollback(candidate, transaction)
