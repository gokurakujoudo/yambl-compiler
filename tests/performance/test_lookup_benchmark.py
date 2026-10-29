"""Exercise the lookup benchmark with small real backend fixtures."""

import unittest

from tests.performance import lookup_benchmark


class LookupBenchmarkTests(unittest.TestCase):
    def test_small_matrix_reports_actual_samples_and_io(self):
        report = lookup_benchmark.run([6], [2, 3], ('sqlite', 'files'), 1)
        self.assertEqual(report['counts'], [6])
        self.assertEqual(report['batch_sizes'], [2, 3])
        self.assertEqual(len(report['samples']), 6)
        self.assertEqual(
            {(row['backend'], row['scenario']) for row in report['samples']},
            {
                (backend, scenario)
                for backend in ('sqlite', 'files')
                for scenario in ('static', 'scd', 'reference')
            },
        )
        for row in report['samples']:
            with self.subTest(backend=row['backend'], scenario=row['scenario']):
                self.assertEqual(row['entities'], 6)
                self.assertEqual(row['dimension_combinations'], 2)
                self.assertEqual({result['batch_size'] for result in row['matching']}, {2, 3})
                for result in row['matching']:
                    self.assertEqual(sum(result['outcomes'].values()), result['batch_size'])
                    self.assertGreaterEqual(result['repeated_inputs'], 1)
                self.assertEqual(set(row['batch_medians']), {'2', '3'})
                self.assertGreater(row['cold'][0]['seconds'], 0)
                self.assertGreater(row['cold'][0]['index_build_seconds'], 0)
                self.assertEqual(set(row['match_median_seconds']), {'2', '3'})
                self.assertGreater(row['memory']['prepare_peak_above_open_bytes'], 0)
                self.assertGreater(row['memory']['after_ten_batches_bytes'], 0)
                if row['backend'] == 'sqlite':
                    self.assertGreater(row['io']['sql_select_statements'], 0)
                    self.assertIsNone(row['io']['jsonl_table_bytes'])
                else:
                    self.assertIsNone(row['io']['sql_select_statements'])
                    self.assertTrue(row['io']['jsonl_table_bytes'])
                    self.assertGreater(sum(row['io']['jsonl_table_bytes'].values()), 0)


if __name__ == '__main__':
    unittest.main()
