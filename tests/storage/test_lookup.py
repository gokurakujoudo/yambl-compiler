"""In-memory match ordering, ambiguity, and invalid input isolation."""

import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from unittest.mock import patch

from tests.support import FilesystemCase, document, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.ir.codec import dumps, loads
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.model import Type
from yambl_compiler.languages.yambl.semantics.builtins import DOCUMENTS, RESULT_CONSTANTS
from yambl_compiler.languages.yambl.semantics.symbols import resolve_scopes
from yambl_compiler.languages.yambl.semantics.validation import analyze, analyze_units
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.lookup import AmbiguousMatch, KeyField, Matcher
from yambl_compiler.storage.lookup_prepare import prepare
from yambl_compiler.storage.results import EntityRef, NonNormalResult, Status, Success
from yambl_compiler.storage.scalars import ScalarRule


@dataclass(frozen=True, slots=True, kw_only=True)
class Key:
    label: str


class Code(StrEnum):
    One = 'One'


class OtherCode(StrEnum):
    One = 'One'


@dataclass(frozen=True, slots=True, kw_only=True)
class CompositeKey:
    amount: Decimal
    day: date
    code: Code
    count: int


class MatcherTests(unittest.TestCase):
    def test_unique_ambiguous_missing_and_invalid_records(self):
        string = Type('string', 'string', '', (), (), None, False, (), ())
        matcher = Matcher(
            Key,
            (KeyField('label', ScalarRule(string)),),
            [(('a',), 7), (('a',), 2), (('a',), 7), (('b',), 9)],
            1,
        )
        result = matcher.match_many(
            [Key(label='b'), Key(label='a'), Key(label='missing'), Key(label='a'), Key(label=3)]
        )
        self.assertEqual(
            result,
            Success(
                (
                    Success(9),
                    AmbiguousMatch((2, 7)),
                    NonNormalResult(Status.NO_ENTRY),
                    AmbiguousMatch((2, 7)),
                    NonNormalResult(Status.ERROR, 'input 4, key label: invalid input for string'),
                )
            ),
        )
        self.assertEqual(
            (
                matcher.stats.scanned_entities,
                matcher.stats.indexed_entities,
                matcher.stats.skipped_entities,
                matcher.stats.distinct_keys,
                matcher.stats.ambiguous_keys,
            ),
            (5, 4, 1, 2, 1),
        )
        self.assertEqual(matcher.match_many([]), Success(()))
        self.assertEqual(
            matcher.match_many('bad'),
            NonNormalResult(Status.ERROR, 'lookup keys must be a sequence'),
        )
        self.assertEqual(
            matcher.match_many([object()]),
            Success((NonNormalResult(Status.ERROR, 'input 0: invalid key record'),)),
        )

    def test_composite_types_and_numeric_equivalence(self):
        scalar = lambda name: Type(name, 'value', '', (), (), None, False, (), ())
        dictionary = Type('D', 'value', 'd.D', (), (), None, False, (), ())
        code = Type('code', 'value', '', (dictionary,), (), None, False, (), ())
        fields = (
            KeyField('amount', ScalarRule(scalar('decimal'))),
            KeyField('day', ScalarRule(scalar('date'))),
            KeyField('code', ScalarRule(code, Code, frozenset({'One'}))),
            KeyField('count', ScalarRule(scalar('integer'))),
        )
        matcher = Matcher(
            CompositeKey, fields, [((Decimal('1.00'), date(2026, 10, 29), Code.One, 3), 'id')], 0
        )
        good = CompositeKey(amount=Decimal('1.0'), day=date(2026, 10, 29), code=Code.One, count=3)
        wrong_code = CompositeKey(
            amount=Decimal('1'), day=date(2026, 10, 29), code=OtherCode.One, count=3
        )
        wrong_count = CompositeKey(
            amount=Decimal('1'), day=date(2026, 10, 29), code=Code.One, count=True
        )
        self.assertEqual(
            matcher.match_many([good, wrong_code, wrong_count]),
            Success(
                (
                    Success('id'),
                    NonNormalResult(Status.ERROR, 'input 1, key code: invalid input for code'),
                    NonNormalResult(Status.ERROR, 'input 2, key count: invalid input for integer'),
                )
            ),
        )


class PreparedLookupTests(FilesystemCase):
    def test_builtin_entity_identity_lookup_on_both_backends(self):
        original = next(item for item in DOCUMENTS if item.path.name == 'hierarchy.yambl')
        source = original.text.replace(
            'id hierarchy_id: string',
            'id hierarchy_id: string\n    lookup by_id { id = self.hierarchy_id::value() }',
            1,
        )
        modified = replace(document(source, str(original.path)), builtin=True)
        builtins = [modified if item.path == original.path else item for item in DOCUMENTS]
        diagnostics = []
        analysis = analyze_units(
            resolve_scopes(
                [document('entity E { id id: string }')], diagnostics, builtins, RESULT_CONSTANTS
            ),
            diagnostics,
        )
        self.assertEqual(analysis.diagnostics, ())
        invalid = replace(
            document(
                source.replace('self.hierarchy_id::value()', 'self.missing::value()'),
                str(original.path),
            ),
            builtin=True,
        )
        invalid_builtins = [invalid if item.path == original.path else item for item in DOCUMENTS]
        invalid_diagnostics = []
        invalid_analysis = analyze_units(
            resolve_scopes(
                [document('entity E { id id: string }')],
                invalid_diagnostics,
                invalid_builtins,
                RESULT_CONSTANTS,
            ),
            invalid_diagnostics,
        )
        self.assertIn(
            'unknown entity field: missing', [item.message for item in invalid_analysis.diagnostics]
        )
        model = Lowering(analysis, Path('.'), '', 'test').model()
        model = loads(dumps(model))
        output = self.root / 'generated'
        layout = generate(model, output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        rule = next(
            item for item in model.definitions if item.identity == 'builtins.Hierarchy'
        ).lookups[0]
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            writer.execute('INSERT INTO tb_hierarchy (hierarchy_id) VALUES (?)', ('h',))
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            id: str

        class InputMatcher(Matcher[Input, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            result = prepare(
                reader._session, 'builtins.Hierarchy', rule, Input, str, {}, InputMatcher
            )
            self.assertIsInstance(result, Success)
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(result.value.match_many([Input(id='h')]), Success((Success('h'),)))

    def test_intermediate_and_terminal_scd_use_independent_dates(self):
        source = """entity Issuer { id id: string @temporal(industry_date: date) scd attr industry: string }
            entity Security { id id: string @temporal(relation_date: date) scd attr issuer: Issuer }"""
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze(
            [
                document(
                    source
                    + " extend Security { lookup by_industry { industry = self.issuer::value(relation_date=input.rdate).industry::value(industry_date=input.idate) } lookup by_fixed { industry = self.issuer::value(relation_date='2025-01-01').industry::value(industry_date='2027-01-01') } }"
                )
            ]
        )
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rules = next(d for d in lookup_model.definitions if d.identity == 'Security').lookups
        rule = next(rule for rule in rules if rule.name == 'by_industry')
        fixed_rule = next(rule for rule in rules if rule.name == 'by_fixed')
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            issuer, industry = layout.fields['Security.issuer'], layout.fields['Issuer.industry']
            writer.execute('INSERT INTO tb_security (id) VALUES (?)', ('s',))
            writer.execute('INSERT INTO tb_issuer (id) VALUES (?)', ('i',))
            for stored, identity, start, end, value in [
                (issuer, 's', '2024-01-01', '2026-01-01', 'i'),
                (industry, 'i', '2026-01-01', '2028-01-01', 'Tech'),
            ]:
                writer.execute(
                    f'INSERT INTO "{stored.table}" (id, valid_from, valid_to, "{stored.state}", "{stored.value}") VALUES (?, ?, ?, ?, ?)',
                    (identity, start, end, 0, value),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            industry: str

        class InputMatcher(Matcher[Input, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            result = prepare(
                reader._session,
                'Security',
                rule,
                Input,
                str,
                {'rdate': date(2025, 1, 1), 'idate': date(2027, 1, 1)},
                InputMatcher,
            )
            self.assertIsInstance(result, Success)
            fixed = prepare(reader._session, 'Security', fixed_rule, Input, str, {}, InputMatcher)
            self.assertIsInstance(fixed, Success)
            self.assertEqual(
                fixed.value.match_many([Input(industry='Tech')]), Success((Success('s'),))
            )
            missing = prepare(
                reader._session,
                'Security',
                rule,
                Input,
                str,
                {'rdate': date(2026, 1, 1), 'idate': date(2027, 1, 1)},
                InputMatcher,
            )
            self.assertIsInstance(missing, Success)
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(
                result.value.match_many([Input(industry='Tech')]), Success((Success('s'),))
            )
            self.assertEqual(
                missing.value.match_many([Input(industry='Tech')]),
                Success((NonNormalResult(Status.NO_ENTRY),)),
            )

    def test_sparse_and_dense_fields_on_both_backends(self):
        source = "entity E { id id: string @sparse('s') attr x: string @dense('d') attr y: string }"
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze(
            [
                document(
                    source
                    + ' extend E { lookup by_xy { x = self.x::value() y = self.y::value() } }'
                )
            ]
        )
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rule = next(d for d in lookup_model.definitions if d.identity == 'E').lookups[0]
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            sparse, dense = layout.fields['E.x'], layout.fields['E.y']
            writer.execute('INSERT INTO tb_e (id) VALUES (?)', ('a',))
            writer.execute(
                f'INSERT INTO "{sparse.table}" (id, field, "{sparse.state}", "{sparse.value}") VALUES (?, ?, ?, ?)',
                ('a', 'E.x', 0, 'alpha'),
            )
            writer.execute(
                f'INSERT INTO "{dense.table}" (id, "{dense.state}", "{dense.value}") VALUES (?, ?, ?)',
                ('a', 0, 'beta'),
            )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            x: str
            y: str

        class InputMatcher(Matcher[Input, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            with patch.object(
                reader._backend, 'rows_many', wraps=reader._backend.rows_many
            ) as calls:
                result = prepare(reader._session, 'E', rule, Input, str, {}, InputMatcher)
                self.assertIsInstance(result, Success)
                self.assertCountEqual(
                    [call.args[0] for call in calls.call_args_list],
                    ['tb_e', sparse.table, dense.table],
                )
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(
                result.value.match_many([Input(x='alpha', y='beta')]), Success((Success('a'),))
            )

        broken = files / 'tables' / (sparse.table + '.jsonl')
        metadata_file = files / 'repository.json'
        metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
        for text, has_id in (('{"id":"a"}\n', True), ('{}\n', False), ('{"id":4}\n', False)):
            broken.write_text(text, encoding='utf-8', newline='\n')
            metadata['tables'][sparse.table] = hashlib.sha256(broken.read_bytes()).hexdigest()
            metadata_file.write_text(json.dumps(metadata), encoding='utf-8')
            opened = module.SampleReader.open_files(files)
            self.assertIsInstance(opened, Success)
            reader = opened.value
            failed = prepare(reader._session, 'E', rule, Input, str, {}, InputMatcher)
            self.assertIsInstance(failed, NonNormalResult)
            self.assertEqual(failed.status, Status.ERROR)
            self.assertIn('key x, field E.x', failed.message)
            self.assertIn(str(broken), failed.message)
            self.assertIn(':1:', failed.message)
            self.assertEqual('entity a' in failed.message, has_id)
            self.assertEqual(reader.close(), Success(None))

    def test_overloaded_code_and_string_use_common_string_key(self):
        source = """decodes Sources { - 1 One: 'One' } entity Base { id id: string }
            entity A inherits Base { @overload attr label: code<Sources> }
            entity B inherits Base { @overload attr label: string }"""
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze(
            [document(source + ' extend Base { lookup by_label { label = self.label::value() } }')]
        )
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rule = next(d for d in lookup_model.definitions if d.identity == 'Base').lookups[0]
        self.assertEqual(rule.keys[0].type.name, 'string')
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            label = layout.fields['Base.label']
            for identity, actual in [('a', 'A'), ('b', 'B')]:
                writer.execute(
                    'INSERT INTO tb_base (id, entity_type) VALUES (?, ?)', (identity, actual)
                )
                writer.execute(
                    f'UPDATE tb_base SET "{label.state}" = ?, "{label.value}" = ? WHERE id = ?',
                    (0, 'One', identity),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            label: str

        class InputMatcher(Matcher[Input, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            result = prepare(reader._session, 'Base', rule, Input, str, {}, InputMatcher)
            self.assertIsInstance(result, Success)
            matcher = result.value
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(
                matcher.match_many([Input(label='One')]), Success((AmbiguousMatch(('a', 'b')),))
            )

    def test_two_dimensions_and_scd_coordinates(self):
        source = """decodes Sources { - 1 Wind: 'Wind' - 2 Other: 'Other' }
            entity E { id id: string dimension source_dim { value source: code<Sources> }
                dimension market_dim { value market: string }
                @dimension(source_dim) @dimension(market_dim) @temporal(day: date) scd attr client: string }
        """
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze(
            [
                document(
                    source
                    + " extend E { lookup by_client { client = self.client::value(source=Sources.Wind, market=input.area) } lookup by_fixed { client = self.client::value(source=Sources.Wind, market='CN') } }"
                )
            ]
        )
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rules = next(d for d in lookup_model.definitions if d.identity == 'E').lookups
        rule = next(rule for rule in rules if rule.name == 'by_client')
        fixed_rule = next(rule for rule in rules if rule.name == 'by_fixed')
        self.assertEqual([item.name for item in rule.inputs], ['area', 'day'])
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            client = layout.fields['E.client']
            writer.execute('INSERT INTO tb_e (id) VALUES (?)', ('a',))
            for market, vendor, value in [
                ('CN', 'Wind', 'match'),
                ('US', 'Wind', 'other'),
                ('CN', 'Other', 'other'),
            ]:
                writer.execute(
                    f'INSERT INTO "{client.table}" (id, dim_source, dim_market, valid_from, valid_to, "{client.state}", "{client.value}") VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('a', vendor, market, '2026-01-01', '2027-01-01', 0, value),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            client: str

        class InputMatcher(Matcher[Input, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            with patch.object(
                reader._backend, 'rows_many', wraps=reader._backend.rows_many
            ) as calls:
                result = prepare(
                    reader._session,
                    'E',
                    rule,
                    Input,
                    str,
                    {'area': 'CN', 'day': date(2026, 10, 29)},
                    InputMatcher,
                )
                self.assertIsInstance(result, Success)
                self.assertCountEqual(
                    [call.args[0] for call in calls.call_args_list],
                    ['tb_decodes', 'tb_decodes_entries', 'tb_e', client.table],
                )
            matcher = result.value
            fixed = prepare(
                reader._session,
                'E',
                fixed_rule,
                Input,
                str,
                {'day': date(2026, 10, 29)},
                InputMatcher,
            )
            self.assertIsInstance(fixed, Success)
            self.assertEqual(
                fixed.value.match_many([Input(client='match')]), Success((Success('a'),))
            )
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(
                matcher.match_many([Input(client='match'), Input(client='other')]),
                Success((Success('a'), NonNormalResult(Status.NO_ENTRY))),
            )

    def test_overloaded_reference_uses_common_identity_in_both_backends(self):
        source = """entity Parent { id id: string } entity One inherits Parent {} entity Two inherits Parent {}
            entity Base { id id: string } entity A inherits Base { @overload attr target: One }
            entity B inherits Base { @overload attr target: Two }"""
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze(
            [
                document(
                    source + ' extend Base { lookup by_target { target = self.target::value() } }'
                )
            ]
        )
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rule = next(d for d in lookup_model.definitions if d.identity == 'Base').lookups[0]
        self.assertEqual(rule.keys[0].type.target, 'Parent')
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            target = layout.fields['Base.target']
            for identity, actual in [('i1', 'One'), ('i2', 'Two')]:
                writer.execute(
                    'INSERT INTO tb_parent (id, entity_type) VALUES (?, ?)', (identity, actual)
                )
            writer.execute('INSERT INTO tb_base (id, entity_type) VALUES (?, ?)', ('root', 'Base'))
            for identity, actual, ref in [('a', 'A', 'i1'), ('b', 'B', 'i2')]:
                writer.execute(
                    'INSERT INTO tb_base (id, entity_type) VALUES (?, ?)', (identity, actual)
                )
                writer.execute(
                    f'UPDATE tb_base SET "{target.state}" = ?, "{target.value}" = ? WHERE id = ?',
                    (0, ref, identity),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            target: EntityRef[object]

        class InputMatcher(Matcher[Input, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            result = prepare(reader._session, 'Base', rule, Input, str, {}, InputMatcher)
            self.assertIsInstance(result, Success)
            matcher = result.value
            self.assertEqual(
                (
                    matcher.stats.scanned_entities,
                    matcher.stats.indexed_entities,
                    matcher.stats.skipped_entities,
                ),
                (3, 2, 1),
            )
            branch = prepare(reader._session, 'A', rule, Input, str, {}, InputMatcher)
            self.assertIsInstance(branch, Success)
            self.assertEqual(
                (branch.value.stats.scanned_entities, branch.value.stats.indexed_entities), (1, 1)
            )
            self.assertEqual(
                branch.value.match_many(
                    [
                        Input(target=EntityRef[object]('Parent', 'i1')),
                        Input(target=EntityRef[object]('Parent', 'i2')),
                    ]
                ),
                Success((Success('a'), NonNormalResult(Status.NO_ENTRY))),
            )
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(
                matcher.match_many(
                    [
                        Input(target=EntityRef[object]('Parent', 'i1')),
                        Input(target=EntityRef[object]('Parent', 'i2')),
                        Input(target=EntityRef[object]('One', 'i2')),
                    ]
                ),
                Success(
                    (
                        Success('a'),
                        Success('b'),
                        NonNormalResult(
                            Status.ERROR,
                            'input 2, key target: dimension requires a reference of the declared type',
                        ),
                    )
                ),
            )

    def test_fixed_code_is_validated_with_no_candidates(self):
        source = "decodes Sources { - 1 Wind: 'Wind' } entity E { id id: string attr source: code<Sources> dimension d { value source: code<Sources> } @dimension(d) attr client: string }"
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze(
            [
                document(
                    source
                    + ' extend E { lookup by_client { client = self.client::value(source=Sources.Wind) } lookup by_source { source = self.source::value() } }'
                )
            ]
        )
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rules = next(d for d in lookup_model.definitions if d.identity == 'E').lookups
        rule = next(rule for rule in rules if rule.name == 'by_client')
        source_rule = next(rule for rule in rules if rule.name == 'by_source')
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            writer.execute(
                "DELETE FROM tb_decodes_entries WHERE decodes_id = 'Sources' AND code = 'Wind'"
            )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            client: str

        class InputMatcher(Matcher[Input, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            failed = prepare(reader._session, 'E', rule, Input, str, {}, InputMatcher)
            self.assertIsInstance(failed, NonNormalResult)
            self.assertEqual(failed.status, Status.ERROR)
            self.assertIn('key client, parameter source: unknown code for Sources', failed.message)
            self.assertEqual(reader.close(), Success(None))

        with closing(sqlite3.connect(database)) as writer:
            writer.execute("UPDATE tb_decodes SET entries__state=1 WHERE decodes_id='Sources'")
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class SourceInput:
            source: str

        class SourceMatcher(Matcher[SourceInput, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            failed = prepare(reader._session, 'E', source_rule, SourceInput, str, {}, SourceMatcher)
            self.assertIsInstance(failed, NonNormalResult)
            self.assertEqual(failed.status, Status.ERROR)
            self.assertIn('key source: invalid dictionary state for Sources', failed.message)
            self.assertEqual(reader.close(), Success(None))

    def test_full_table_projection_both_backends_and_detached_matcher(self):
        source = 'entity E { id id: string attr name: string attr market: string } entity EChild inherits E {}'
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        model = compile_repository(self.root)
        output = self.root / 'generated'
        layout = generate(model, output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze(
            [
                document(
                    source
                    + ' extend E { lookup by_name { name = self.name::value() market = self.market::value() } lookup by_id { id = self.id::value() } }'
                )
            ]
        )
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rules = next(d for d in lookup_model.definitions if d.identity == 'E').lookups
        rule = next(rule for rule in rules if rule.name == 'by_name')
        id_rule = next(rule for rule in rules if rule.name == 'by_id')
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            name = layout.fields['E.name']
            market = layout.fields['E.market']
            for identity, label, exchange in [
                ('a', 'same', 'A'),
                ('b', 'same', 'A'),
                ('c', 'other', 'B'),
            ]:
                writer.execute('INSERT INTO tb_e (id, entity_type) VALUES (?, ?)', (identity, 'E'))
                writer.execute(
                    f'UPDATE tb_e SET "{name.state}" = ?, "{name.value}" = ?, "{market.state}" = ?, "{market.value}" = ? WHERE id = ?',
                    (0, label, 0, exchange, identity),
                )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            name: str
            market: str

        class InputMatcher(Matcher[Input, str]):
            pass

        class WrongIdMatcher(Matcher[Input, int]):
            pass

        @dataclass(frozen=True, slots=True, kw_only=True)
        class IdInput:
            id: str

        class IdMatcher(Matcher[IdInput, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            with patch.object(
                reader._backend, 'rows_many', wraps=reader._backend.rows_many
            ) as calls:
                result = prepare(reader._session, 'E', rule, Input, str, {}, InputMatcher)
                self.assertIsInstance(result, Success)
                self.assertIsInstance(result.value, InputMatcher)
                self.assertEqual([call.args[0] for call in calls.call_args_list], ['tb_e'])
            matcher = result.value
            wrong_id = prepare(reader._session, 'E', rule, Input, int, {}, WrongIdMatcher)
            self.assertIsInstance(wrong_id, NonNormalResult)
            self.assertIn('invalid candidate ID type', wrong_id.message)
            ids = prepare(reader._session, 'E', id_rule, IdInput, str, {}, IdMatcher)
            self.assertIsInstance(ids, Success)
            self.assertEqual(
                ids.value.match_many([IdInput(id='a'), IdInput(id='missing')]),
                Success((Success('a'), NonNormalResult(Status.NO_ENTRY))),
            )
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(
                matcher.match_many(
                    [
                        Input(name='same', market='A'),
                        Input(name='other', market='B'),
                        Input(name='none', market='A'),
                    ]
                ),
                Success(
                    (AmbiguousMatch(('a', 'b')), Success('c'), NonNormalResult(Status.NO_ENTRY))
                ),
            )
            self.assertEqual(
                (
                    matcher.stats.scanned_entities,
                    matcher.stats.indexed_entities,
                    matcher.stats.skipped_entities,
                ),
                (3, 3, 0),
            )

        with closing(sqlite3.connect(database)) as writer:
            writer.execute('PRAGMA ignore_check_constraints = ON')
            writer.execute('INSERT INTO tb_e (id, entity_type) VALUES (?, ?)', ('bad', 'Missing'))
            writer.commit()
        opened = module.SampleReader.open_sqlite(database)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        failed = prepare(reader._session, 'E', rule, Input, str, {}, InputMatcher)
        self.assertIsInstance(failed, NonNormalResult)
        self.assertEqual(failed.status, Status.ERROR)
        self.assertIn('table tb_e', failed.message)
        self.assertIn('entity bad: invalid actual entity type', failed.message)
        self.assertEqual(reader.close(), Success(None))

        with closing(sqlite3.connect(database)) as writer:
            name = layout.fields['E.name']
            writer.execute('DROP TABLE tb_e')
            writer.execute(
                f'CREATE TABLE tb_e (id TEXT, entity_type TEXT, "{name.state}" INTEGER, "{name.value}" TEXT, "{market.state}" INTEGER, "{market.value}" TEXT)'
            )
            writer.executemany(
                'INSERT INTO tb_e VALUES (?, ?, ?, ?, ?, ?)',
                [('a', 'E', 0, 'x', 0, 'y'), ('a', 'E', 0, 'x', 0, 'y')],
            )
            writer.commit()
        opened = module.SampleReader.open_sqlite(database)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        failed = prepare(reader._session, 'E', rule, Input, str, {}, InputMatcher)
        self.assertIsInstance(failed, NonNormalResult)
        self.assertIn('table tb_e, entity a: duplicate identity', failed.message)
        self.assertEqual(reader.close(), Success(None))

    def test_scd_reference_paths_and_missing_candidates(self):
        source = """entity Issuer { id id: string attr name: string }
            entity Security { id id: string attr issuer: Issuer @temporal(day: date) scd attr client: string }
        """
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('main.yambl', source)
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        analysis = analyze([document(source + """extend Security { lookup by_client {
            issuer = self.issuer.name::value()
            client = self.client::value(day=input.when)
        } lookup by_issuer { issuer = self.issuer::value() } }""")])
        lookup_model = Lowering(analysis, Path('.'), '', 'test').model()
        rules = next(d for d in lookup_model.definitions if d.identity == 'Security').lookups
        rule = next(rule for rule in rules if rule.name == 'by_client')
        reference_rule = next(rule for rule in rules if rule.name == 'by_issuer')
        database = self.root / 'test.sqlite'
        with closing(sqlite3.connect(database)) as writer:
            writer.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            name = layout.fields['Issuer.name']
            issuer = layout.fields['Security.issuer']
            client = layout.fields['Security.client']
            writer.execute('INSERT INTO tb_issuer (id) VALUES (?)', ('i',))
            writer.execute(
                f'UPDATE tb_issuer SET "{name.state}" = ?, "{name.value}" = ? WHERE id = ?',
                (0, 'Acme', 'i'),
            )
            for identity, ref in [('a', 'i'), ('b', 'i'), ('c', 'missing')]:
                writer.execute(
                    'INSERT INTO tb_security (id) VALUES (?)',
                    (identity,),
                )
                writer.execute(
                    f'UPDATE tb_security SET "{issuer.state}" = ?, "{issuer.value}" = ? WHERE id = ?',
                    (0, ref, identity),
                )
            writer.execute(
                f'INSERT INTO "{client.table}" (id, valid_from, valid_to, "{client.state}", "{client.value}") VALUES (?, ?, ?, ?, ?)',
                ('a', '2026-01-01', '2027-01-01', 0, '123'),
            )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

        @dataclass(frozen=True, slots=True, kw_only=True)
        class Input:
            issuer: str
            client: str

        @dataclass(frozen=True, slots=True, kw_only=True)
        class IssuerInput:
            issuer: EntityRef[object]

        class InputMatcher(Matcher[Input, str]):
            pass

        class IssuerMatcher(Matcher[IssuerInput, str]):
            pass

        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            read_fields = []

            def point(stored, field, rows, when):
                read_fields.append(field.identity)
                return reader._session.point(stored, field, rows, when)

            session = replace(reader._session, point=point)
            missing_input = prepare(session, 'Security', rule, Input, str, {}, InputMatcher)
            self.assertIsInstance(missing_input, NonNormalResult)
            self.assertEqual(missing_input.status, Status.ERROR)
            self.assertIn(
                'preparation parameters do not match lookup contract', missing_input.message
            )
            bad_date = prepare(
                session, 'Security', rule, Input, str, {'when': '2026-10-29'}, InputMatcher
            )
            self.assertIsInstance(bad_date, NonNormalResult)
            self.assertEqual(bad_date.status, Status.ERROR)
            self.assertIn('parameter when: invalid input for date', bad_date.message)
            with patch.object(
                reader._backend, 'rows_many', wraps=reader._backend.rows_many
            ) as calls:
                result = prepare(
                    session,
                    'Security',
                    rule,
                    Input,
                    str,
                    {'when': date(2026, 10, 29)},
                    InputMatcher,
                )
                self.assertIsInstance(result, Success)
                tables = [call.args[0] for call in calls.call_args_list]
                self.assertCountEqual(tables, ['tb_security', 'tb_issuer', client.table])
                self.assertEqual(read_fields.count('Issuer.name'), 1)
            matcher = result.value
            reference_result = prepare(
                session, 'Security', reference_rule, IssuerInput, str, {}, IssuerMatcher
            )
            self.assertIsInstance(reference_result, Success)
            self.assertEqual(
                (
                    reference_result.value.stats.indexed_entities,
                    reference_result.value.stats.skipped_entities,
                ),
                (2, 1),
            )
            self.assertEqual(
                reference_result.value.match_many(
                    [
                        IssuerInput(issuer=EntityRef[object]('Issuer', 'i')),
                        IssuerInput(issuer=EntityRef[object]('Issuer', 'missing')),
                    ]
                ),
                Success((AmbiguousMatch(('a', 'b')), NonNormalResult(Status.NO_ENTRY))),
            )
            boundary = prepare(
                session, 'Security', rule, Input, str, {'when': date(2027, 1, 1)}, InputMatcher
            )
            self.assertIsInstance(boundary, Success)
            self.assertEqual(
                (boundary.value.stats.indexed_entities, boundary.value.stats.skipped_entities),
                (0, 3),
            )
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(
                matcher.match_many([Input(issuer='Acme', client='123')]), Success((Success('a'),))
            )
            self.assertEqual(
                (
                    matcher.stats.scanned_entities,
                    matcher.stats.indexed_entities,
                    matcher.stats.skipped_entities,
                ),
                (3, 1, 2),
            )

        with closing(sqlite3.connect(database)) as writer:
            writer.execute(
                f'INSERT INTO "{client.table}" (id, valid_from, valid_to, "{client.state}", "{client.value}") VALUES (?, ?, ?, ?, ?)',
                ('a', '2026-10-01', '2026-12-01', 0, '456'),
            )
            writer.commit()
            writer.row_factory = sqlite3.Row
            rows = {
                table.name: [dict(row) for row in writer.execute(f'SELECT * FROM "{table.name}"')]
                for table in layout.tables
            }
        for name, text in repository_contents(layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')
        for opened in (
            module.SampleReader.open_sqlite(database),
            module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            failed = prepare(
                reader._session,
                'Security',
                rule,
                Input,
                str,
                {'when': date(2026, 10, 29)},
                InputMatcher,
            )
            self.assertIsInstance(failed, NonNormalResult)
            self.assertEqual(failed.status, Status.ERROR)
            self.assertIn(
                'key client, entity a, field Security.client: overlapping records', failed.message
            )
            self.assertEqual(reader.close(), Success(None))
        self.assertEqual(
            matcher.match_many([Input(issuer='Acme', client='123')]), Success((Success('a'),))
        )

        with closing(sqlite3.connect(database)) as writer:
            writer.execute(f'DELETE FROM "{client.table}" WHERE id = ?', ('a',))
            writer.execute(
                f'INSERT INTO "{client.table}" (id, valid_from, valid_to, "{client.state}", "{client.value}") VALUES (?, ?, ?, ?, ?)',
                ('a', '2026-13-01', '2027-01-01', 0, 'invalid'),
            )
            writer.commit()
        opened = module.SampleReader.open_sqlite(database)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        failed = prepare(
            reader._session,
            'Security',
            rule,
            Input,
            str,
            {'when': date(2026, 10, 29)},
            InputMatcher,
        )
        self.assertIsInstance(failed, NonNormalResult)
        self.assertEqual(failed.status, Status.ERROR)
        self.assertIn('key client, entity a, field Security.client', failed.message)
        self.assertEqual(reader.close(), Success(None))


if __name__ == '__main__':
    unittest.main()
