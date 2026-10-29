"""Deterministic lookup I/O checks against both real storage backends."""

import sqlite3
import unittest
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

from tests.support import FilesystemCase, document, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.languages.yambl.semantics.validation import analyze
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.lookup import Matcher
from yambl_compiler.storage.lookup_prepare import prepare
from yambl_compiler.storage.results import NonNormalResult, Status, Success


@dataclass(frozen=True, slots=True, kw_only=True)
class LookupKey:
    label: str
    market: str
    issuer_name: str


class LookupIOTests(FilesystemCase):
    def test_projection_and_shared_reference_work_do_not_scale_with_candidates(self):
        source = '''entity Issuer { id id: string attr name: string }
entity Security { id id: string attr label: string attr market: string attr issuer: Issuer }
'''
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', source)
        model = compile_repository(self.root)
        output = self.root / 'generated'
        layout = generate(model, output, 'sample_model')
        generated = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze([document(source + '''extend Security {
    lookup by_details {
        label = self.label::value()
        market = self.market::value()
        issuer_name = self.issuer::value().name::value()
    }
}''')])
        self.assertEqual([diagnostic.message for diagnostic in analysis.diagnostics], [])
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rule = next(
            item for item in lookup_model.definitions if item.identity == 'Security'
        ).lookups[0]
        observations = []
        for count in (10, 100):
            database = self.root / f'{count}.sqlite'
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
                connection.execute(
                    "INSERT INTO tb_issuer (id, entity_type, name__state, name) VALUES ('i', 'Issuer', 'VALUE', 'shared')"
                )
                connection.executemany(
                    "INSERT INTO tb_security (id, entity_type, label__state, label, market__state, market, issuer__state, issuer) VALUES (?, 'Security', 'VALUE', ?, 'VALUE', 'market', 'VALUE', 'i')",
                    [(str(index), f'label-{index}') for index in range(count)],
                )
                connection.commit()
                connection.row_factory = sqlite3.Row
                rows = {
                    table.name: [
                        dict(row) for row in connection.execute(f'SELECT * FROM "{table.name}"')
                    ]
                    for table in layout.tables
                }
            files = self.root / f'files-{count}'
            for relative, content in repository_contents(layout, rows).items():
                destination = files / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding='utf-8', newline='\n')
            for backend, opened in (
                ('sqlite', generated.SampleReader.open_sqlite(database)),
                ('files', generated.SampleReader.open_files(files)),
            ):
                self.assertIsInstance(opened, Success)
                reader = opened.value
                point_calls = Counter()
                original_point = reader._session.point

                def point(stored, field, records, when):
                    point_calls[field.identity] += 1
                    return original_point(stored, field, records, when)

                session = replace(reader._session, point=point)
                try:
                    with patch.object(
                        reader._backend, 'rows_many', wraps=reader._backend.rows_many
                    ) as calls:
                        result = prepare(session, 'Security', rule, LookupKey, str, {}, Matcher)
                        self.assertIsInstance(result, Success)
                        tables = Counter(call.args[0] for call in calls.call_args_list)
                        projected = {
                            call.args[0]: set(call.args[2]) for call in calls.call_args_list
                        }
                    matcher = result.value
                    self.assertEqual(tables, {'tb_security': 1, 'tb_issuer': 1})
                    self.assertEqual(
                        projected['tb_security'],
                        {
                            'id',
                            'entity_type',
                            'label__state',
                            'label',
                            'market__state',
                            'market',
                            'issuer__state',
                            'issuer',
                        },
                    )
                    self.assertEqual(
                        projected['tb_issuer'], {'id', 'entity_type', 'name__state', 'name'}
                    )
                    self.assertEqual(point_calls['Issuer.name'], 1)
                    self.assertEqual(matcher.stats.scanned_entities, count)
                    self.assertEqual(matcher.stats.indexed_entities, count)
                    reference = {}
                    for index in range(count):
                        found = reader.get_security(str(index))
                        self.assertIsInstance(found, Success)
                        security = found.value
                        label = security.read_label()
                        market = security.read_market()
                        issuer_ref = security.read_issuer()
                        self.assertIsInstance(label, Success)
                        self.assertIsInstance(market, Success)
                        self.assertIsInstance(issuer_ref, Success)
                        issuer = reader.resolve(issuer_ref.value)
                        self.assertIsInstance(issuer, Success)
                        issuer_name = issuer.value.read_name()
                        self.assertIsInstance(issuer_name, Success)
                        key = LookupKey(
                            label=label.value, market=market.value, issuer_name=issuer_name.value
                        )
                        reference[key] = str(index)
                    self.assertEqual(
                        matcher.match_many(list(reference)),
                        Success(tuple(Success(identity) for identity in reference.values())),
                    )
                    self.assertEqual(reader.close(), Success(None))
                    keys = [
                        LookupKey(label='label-0', market='market', issuer_name='shared'),
                        LookupKey(label='unknown', market='market', issuer_name='shared'),
                    ]
                    index_before = dict(matcher._index)
                    attempts = Counter()

                    def block_io(name):
                        def blocked(*args, **kwargs):
                            attempts[name] += 1
                            raise AssertionError(f'hot lookup attempted {name}')

                        return blocked

                    with (
                        patch('builtins.open', side_effect=block_io('open')),
                        patch('io.open', side_effect=block_io('io.open')),
                        patch('os.open', side_effect=block_io('os.open')),
                        patch('sqlite3.connect', side_effect=block_io('sqlite3.connect')),
                    ):
                        hot_results = [matcher.match_many(keys) for _ in range(3)]
                    self.assertEqual(attempts, {})
                    self.assertEqual(
                        hot_results,
                        [Success((Success('0'), NonNormalResult(Status.NO_ENTRY)))] * 3,
                    )
                    self.assertEqual(dict(matcher._index), index_before)
                    observations.append((count, backend, dict(tables), point_calls['Issuer.name']))
                finally:
                    if not reader._closed:
                        reader.close()
        self.assertEqual(len(observations), 4)


if __name__ == '__main__':
    unittest.main()
