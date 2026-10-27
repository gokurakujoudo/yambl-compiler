"""Verify detached candidate reads retain batching and snapshot semantics."""

from unittest import TestCase

from yambl_compiler.storage.backends import Selection
from yambl_compiler.transactions.snapshot import SnapshotBackend


class SnapshotBackendTests(TestCase):
    def test_batch_selection_projection_and_detachment(self):
        tables = {
            'prices': [
                {'id': 'a', 'date': '2026-01-01', 'value': 1},
                {'id': 'a', 'date': '2026-01-02', 'value': 2},
                {'id': 'b', 'date': '2026-01-02', 'value': 3},
            ],
            'periods': [
                {'id': 'a', 'valid_from': '2026-01-01', 'valid_to': '2026-01-03', 'value': 4},
                {'id': 'a', 'valid_from': '2026-01-03', 'valid_to': '2026-01-05', 'value': 5},
            ],
        }
        backend = SnapshotBackend(tables)
        tables['prices'][0]['value'] = 99
        self.assertEqual(backend.rows('prices', {'id': 'a'})[0]['value'], 1)
        selected = backend.rows_many(
            'prices',
            [
                Selection((('id', 'a'),), 'snapshot', '2026-01-01', '2026-01-02'),
                Selection((('id', 'b'),), 'snapshot', '2026-01-01', '2026-01-01'),
                Selection((('id', 'a'),), 'snapshot', '2026-01-01', '2026-01-02'),
            ],
            ('value',),
        )
        self.assertEqual(selected, [[{'value': 1}, {'value': 2}], [], [{'value': 1}, {'value': 2}]])
        selected[0][0]['value'] = 100
        self.assertEqual(backend.rows('prices', {'id': 'a'})[0]['value'], 1)
        self.assertEqual(
            backend.rows_many(
                'periods',
                [Selection((('id', 'a'),), 'scd', '2026-01-03', '2026-01-03')],
                ('value',),
            ),
            [[{'value': 5}]],
        )
        self.assertEqual(
            backend.rows_many('prices', [Selection((('id', 'b'),))]),
            [[{'id': 'b', 'date': '2026-01-02', 'value': 3}]],
        )
        backend.close()
