"""Exercise generated lookup APIs against real repositories."""

import ast
import inspect
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from tests.support import FilesystemCase, document, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.languages.yambl.semantics.validation import analyze
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.extras import python_reference
from yambl_compiler.storage.generation import generate, python_package
from yambl_compiler.storage.layout import build_layout, quote, table_name
from yambl_compiler.storage.lookup_prepare import prepare
from yambl_compiler.storage.rendering import ddl
from yambl_compiler.storage.results import NonNormalResult, Status, Success

SOURCE = '''namespace core
entity Security {
    id security_id: string
    static attr name: string
    @temporal(day: date) scd attr code: string
    lookup by_name { name = self.name::value() }
    lookup by_code { code = self.code::value(day=input.as_of_date) }
}
entity Bond inherits Security {}
'''

OVERLOAD_SOURCE = '''decodes Sources { - 1 Primary: 'Primary' }
entity Parent { id id: string } entity One inherits Parent {} entity Two inherits Parent {}
entity Base { id id: string lookup by_target { target = self.target::value() } lookup by_label { label = self.label::value() } }
entity A inherits Base { @overload attr target: One @overload attr label: code<Sources> lookup by_specific { target = self.target::value() label = self.label::value() } }
entity B inherits Base { @overload attr target: Two @overload attr label: string }'''


class LookupGenerationTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('lookup.yambl', SOURCE)
        self.model = compile_repository(self.root)
        self.layout = build_layout(self.model)

    def generated(self, model=None):
        model = model or self.model
        output = self.root / 'generated'
        generate(model, output, 'sample_model')
        return load_generated(output / 'sample_model', 'sample_model')

    def file_copy(self, layout, database):
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                table.name: [
                    dict(row) for row in connection.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in layout.tables
            }
        files = self.root / 'files'
        for name, content in repository_contents(layout, tables).items():
            target = files / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
        return files

    def test_dimensioned_static_override_and_two_reference_steps(self):
        source = '''
entity Country { id id: string attr name: string }
entity Issuer { id id: string attr country: Country }
entity Security {
    id id: string
    dimension markets { value market: string }
    @dimension(markets) attr external: string
    attr label: string
    attr issuer: Issuer
    lookup by_detail {
        label = self.label::value()
        external = self.external::value(market=input.market)
        country = self.issuer.country.name::value()
    }
}
entity Bond inherits Security { @override attr label: exact<'fixed'> }
'''
        analysis = analyze([document(source, 'paths.yambl')])
        self.assertEqual([diagnostic.message for diagnostic in analysis.diagnostics], [])
        model = Lowering(analysis, Path('.'), '', 'test').model()
        layout = build_layout(model)
        module = self.generated(model)
        database = self.root / 'paths.sqlite'
        external = layout.fields['Security.external']
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(ddl(layout))
            connection.execute(
                "INSERT INTO tb_country (id, entity_type, name__state, name) VALUES ('country', 'Country', 'VALUE', 'China')"
            )
            connection.execute(
                "INSERT INTO tb_issuer (id, entity_type, country__state, country) VALUES ('issuer', 'Issuer', 'VALUE', 'country')"
            )
            connection.executemany(
                'INSERT INTO tb_security (id, entity_type, label__state, label, issuer__state, issuer) VALUES (?, ?, ?, ?, ?, ?)',
                [
                    ('root', 'Security', 'VALUE', 'ordinary', 'VALUE', 'issuer'),
                    ('bond', 'Bond', 'VALUE', 'fixed', 'VALUE', 'issuer'),
                    ('missing', 'Security', 'NO_ENTRY', None, 'NO_ENTRY', None),
                ],
            )
            connection.executemany(
                f'INSERT INTO {quote(external.table)} (id, dim_market, {quote(external.state)}, {quote(external.value)}) VALUES (?, ?, ?, ?)',
                [
                    ('root', 'CN', 'VALUE', 'cn-root'),
                    ('root', 'US', 'VALUE', 'us-root'),
                    ('bond', 'CN', 'VALUE', 'cn-bond'),
                ],
            )
            connection.commit()
        files = self.file_copy(layout, database)
        for opened in (module.SampleReader.open(database), module.SampleReader.open_files(files)):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            china = reader.prepare_security_by_detail(market='CN')
            america = reader.prepare_security_by_detail(market='US')
            bonds = reader.prepare_bond_by_detail(market='CN')
            self.assertIsInstance(china, Success)
            self.assertIsInstance(america, Success)
            self.assertIsInstance(bonds, Success)
            self.assertEqual(reader.close(), Success(None))
            keys = [
                module.SecurityByDetailKey(label='ordinary', external='cn-root', country='China'),
                module.SecurityByDetailKey(label='fixed', external='cn-bond', country='China'),
                module.SecurityByDetailKey(label='missing', external='missing', country='China'),
            ]
            self.assertEqual(
                china.value.match_many(keys),
                Success((Success('root'), Success('bond'), NonNormalResult(Status.NO_ENTRY))),
            )
            self.assertEqual(
                america.value.match_many(
                    [
                        module.SecurityByDetailKey(
                            label='ordinary', external='us-root', country='China'
                        ),
                        keys[1],
                    ]
                ),
                Success((Success('root'), NonNormalResult(Status.NO_ENTRY))),
            )
            self.assertEqual(
                bonds.value.match_many(keys[:2]),
                Success((NonNormalResult(Status.NO_ENTRY), Success('bond'))),
            )
            for matcher, expected in (
                (china.value, (3, 2, 1, 2, 0)),
                (america.value, (3, 1, 2, 1, 0)),
                (bonds.value, (1, 1, 0, 1, 0)),
            ):
                stats = matcher.stats
                self.assertEqual(
                    (
                        stats.scanned_entities,
                        stats.indexed_entities,
                        stats.skipped_entities,
                        stats.distinct_keys,
                        stats.ambiguous_keys,
                    ),
                    expected,
                )

    def test_generated_typed_keys_are_validated_on_both_backends(self):
        source = '''
decodes AlphaCodes { - 1 One: 'One' }
decodes BetaCodes { - 1 One: 'One' }
entity Asset {
    id id: string
    attr amount: decimal
    attr day: date
    attr active: bool
    attr count: integer
    attr alpha: code<AlphaCodes>
    attr beta: code<BetaCodes>
    lookup by_values {
        amount = self.amount::value()
        day = self.day::value()
        active = self.active::value()
        count = self.count::value()
        alpha = self.alpha::value()
        beta = self.beta::value()
    }
}
'''
        analysis = analyze([document(source, 'types.yambl')])
        self.assertEqual([diagnostic.message for diagnostic in analysis.diagnostics], [])
        model = Lowering(analysis, Path('.'), '', 'test').model()
        layout = build_layout(model)
        module = self.generated(model)
        database = self.root / 'types.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(ddl(layout))
            connection.execute(
                'INSERT INTO tb_asset (id, entity_type, amount__state, amount, day__state, day, active__state, active, count__state, count, alpha__state, alpha, beta__state, beta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    'asset',
                    'Asset',
                    'VALUE',
                    '1.00',
                    'VALUE',
                    '2026-10-28',
                    'VALUE',
                    1,
                    'VALUE',
                    '3',
                    'VALUE',
                    'One',
                    'VALUE',
                    'One',
                ),
            )
            connection.commit()
        files = self.file_copy(layout, database)
        valid = module.AssetByValuesKey(
            amount=Decimal('1'),
            day=date(2026, 10, 28),
            active=True,
            count=3,
            alpha=module.AlphaCodesCode.One,
            beta=module.BetaCodesCode.One,
        )
        keys = [
            valid,
            replace(valid, count=True),
            replace(valid, active=1),
            replace(valid, day='2026-10-28'),
            replace(valid, day=datetime(2026, 10, 28)),
            replace(valid, amount='1'),
            replace(valid, alpha=module.BetaCodesCode.One),
            replace(valid, beta=module.AlphaCodesCode.One),
            valid,
        ]
        expected = Success(
            (
                Success('asset'),
                NonNormalResult(Status.ERROR, 'input 1, key count: invalid input for integer'),
                NonNormalResult(Status.ERROR, 'input 2, key active: invalid input for bool'),
                NonNormalResult(Status.ERROR, 'input 3, key day: invalid input for date'),
                NonNormalResult(Status.ERROR, 'input 4, key day: invalid input for date'),
                NonNormalResult(Status.ERROR, 'input 5, key amount: invalid input for decimal'),
                NonNormalResult(Status.ERROR, 'input 6, key alpha: invalid input for code'),
                NonNormalResult(Status.ERROR, 'input 7, key beta: invalid input for code'),
                Success('asset'),
            )
        )
        for opened in (module.SampleReader.open(database), module.SampleReader.open_files(files)):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            prepared = reader.prepare_asset_by_values()
            self.assertIsInstance(prepared, Success)
            self.assertEqual(reader.close(), Success(None))
            self.assertEqual(prepared.value.match_many(keys), expected)

    def test_explicit_signatures_and_inherited_types(self):
        module = self.generated()
        key = module.SecurityByCodeKey(code='X')
        self.assertEqual([(field.name, field.type) for field in fields(key)], [('code', 'str')])
        with self.assertRaises(FrozenInstanceError):
            key.code = 'Y'
        with self.assertRaises(TypeError):
            module.SecurityByCodeKey('X')
        self.assertEqual(
            inspect.signature(module.SampleReader.prepare_security_by_code)
            .parameters['as_of_date']
            .kind,
            inspect.Parameter.KEYWORD_ONLY,
        )
        self.assertIn('SecurityByCodeKey', module.__all__)
        self.assertIn('SecurityByCodeMatcher', module.__all__)
        self.assertIn('AmbiguousMatch', module.__all__)
        reference = python_reference(python_package(self.layout, 'sample_model'), 'sample_model')
        self.assertIn('SecurityByCodeMatcher.match_many', reference)
        self.assertEqual(
            inspect.signature(module.SampleReader.prepare_bond_by_code).return_annotation,
            'Success[SecurityByCodeMatcher] | NonNormalResult',
        )

    def test_overloaded_keys_use_common_public_types(self):
        analysis = analyze([document(OVERLOAD_SOURCE, 'overload.yambl')])
        self.assertEqual(
            [diagnostic.message for diagnostic in analysis.diagnostics],
            [],
        )
        model = Lowering(analysis, Path('.'), '', 'test').model()
        rendered = ast.parse(python_package(build_layout(model), 'sample_model'))
        classes = {node.name: node for node in rendered.body if isinstance(node, ast.ClassDef)}
        target = classes['BaseByTargetKey'].body[-1]
        label = classes['BaseByLabelKey'].body[-1]
        self.assertEqual(ast.unparse(target.annotation), 'EntityRef[Parent]')
        self.assertEqual(ast.unparse(label.annotation), 'str')
        specific = classes['ABySpecificKey']
        self.assertEqual(ast.unparse(specific.body[-2].annotation), 'EntityRef[One]')
        self.assertEqual(ast.unparse(specific.body[-1].annotation), 'SourcesCode')

    def test_overloaded_reference_and_code_match_generated_common_keys(self):
        analysis = analyze([document(OVERLOAD_SOURCE, 'overload.yambl')])
        self.assertEqual(
            [diagnostic.message for diagnostic in analysis.diagnostics],
            [],
        )
        model = Lowering(analysis, Path('.'), '', 'test').model()
        layout = build_layout(model)
        module = self.generated(model)
        database = self.root / 'overload.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(ddl(layout))
            connection.executemany(
                'INSERT INTO tb_parent (id, entity_type) VALUES (?, ?)',
                [('p1', 'One'), ('p2', 'Two')],
            )
            connection.executemany(
                'INSERT INTO tb_base (id, entity_type, target__state, target, label__state, label) VALUES (?, ?, ?, ?, ?, ?)',
                [
                    ('a', 'A', 'VALUE', 'p1', 'VALUE', 'Primary'),
                    ('b', 'B', 'VALUE', 'p2', 'VALUE', 'Primary'),
                ],
            )
            connection.commit()
        opened = module.SampleReader.open(database)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        target = reader.prepare_base_by_target()
        label = reader.prepare_base_by_label()
        self.assertIsInstance(target, Success)
        self.assertIsInstance(label, Success)
        reader.close()
        target_result = target.value.match_many(
            [module.BaseByTargetKey(target=module.EntityRef('Parent', 'p2'))]
        )
        self.assertIsInstance(target_result, Success)
        self.assertEqual(target_result.value, (Success('b'),))
        label_result = label.value.match_many([module.BaseByLabelKey(label='Primary')])
        self.assertIsInstance(label_result, Success)
        self.assertEqual(label_result.value[0].candidates, ('a', 'b'))

    def test_python_name_collisions_report_source(self):
        for source, message in [
            (
                'entity A { id id: string lookup by_name { class = self.id::value() } }',
                'invalid Python lookup key name: class',
            ),
            (
                'entity A { id id: string @temporal(day: date) scd attr value: string lookup by_value { value = self.value::value(day=input.for) } }',
                'invalid Python lookup parameter name: for',
            ),
            (
                'entity A { id id: string lookup by_name { name = self.id::value() } } entity AByNameKey { id id: string }',
                'generated Python public-name collision: AByNameKey',
            ),
            (
                'entity Base { id id: string lookup bar_x { key = self.id::value() } } '
                'entity Foo inherits Base {} '
                'entity FooBar inherits Base { lookup x { key = self.id::value() } }',
                'generated reader method collision: prepare_foo_bar_x',
            ),
        ]:
            with self.subTest(source=source):
                analysis = analyze([document(source, 'collision.yambl')])
                self.assertEqual(
                    [diagnostic.message for diagnostic in analysis.diagnostics],
                    [],
                )
                model = Lowering(analysis, Path('.'), '', 'test').model()
                with self.assertRaisesRegex(ValueError, 'collision.yambl:1:.*' + message):
                    python_package(build_layout(model), 'sample_model')

    def test_integer_identity_is_preserved_in_generated_matcher(self):
        source = SOURCE.replace('id security_id: string', 'id security_id: integer')
        analysis = analyze([document(source, 'integer.yambl')])
        self.assertEqual(
            [diagnostic.message for diagnostic in analysis.diagnostics],
            [],
        )
        model = Lowering(analysis, Path('.'), '', 'test').model()
        layout = build_layout(model)
        module = self.generated(model)
        database = self.root / 'integer.sqlite'
        stored = layout.fields['core.Security.name']
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(ddl(layout))
            connection.execute(
                f'INSERT INTO {quote(table_name("core.Security"))} (id, entity_type, {quote(stored.state)}, {quote(stored.value)}) VALUES (?, ?, ?, ?)',
                ('42', 'core.Security', 'VALUE', 'Alpha'),
            )
            connection.commit()
        opened = module.SampleReader.open(database)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        prepared = reader.prepare_security_by_name()
        self.assertIsInstance(prepared, Success)
        reader.close()
        result = prepared.value.match_many([module.SecurityByNameKey(name='Alpha')])
        self.assertIsInstance(result, Success)
        self.assertEqual(result.value, (Success(42),))
        self.assertIs(type(result.value[0].value), int)

    def test_preparation_parameter_named_rule_is_not_shadowed(self):
        source = SOURCE.replace('day=input.as_of_date', 'day=input.rule')
        analysis = analyze([document(source, 'rule-name.yambl')])
        self.assertEqual(analysis.diagnostics, ())
        model = Lowering(analysis, Path('.'), '', 'test').model()
        module = self.generated(model)
        database = self.root / 'rule.sqlite'
        layout = build_layout(model)
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(ddl(layout))
            connection.row_factory = sqlite3.Row
            tables = {
                table.name: [
                    dict(row) for row in connection.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in layout.tables
            }
        files = self.root / 'empty-files'
        for name, content in repository_contents(layout, tables).items():
            target = files / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
        for opened in (module.SampleReader.open(database), module.SampleReader.open_files(files)):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            result = reader.prepare_security_by_code(rule=date(2026, 10, 28))
            self.assertIsInstance(result, Success)
            stats = result.value.stats
            self.assertEqual(
                (
                    stats.scanned_entities,
                    stats.indexed_entities,
                    stats.skipped_entities,
                    stats.distinct_keys,
                    stats.ambiguous_keys,
                ),
                (0, 0, 0, 0, 0),
            )
            matched = result.value.match_many([module.SecurityByCodeKey(code='X')])
            self.assertIsInstance(matched, Success)
            self.assertEqual(matched.value[0].status, Status.NO_ENTRY)
            reader.close()

    def test_lookup_optional_default_and_coalescing_on_both_backends(self):
        source = SOURCE.replace(
            'lookup by_code { code = self.code::value(day=input.as_of_date) }',
            "@extra_input_param(industry_date: date, 'Optional date') "
            "@extra_input_param(as_of_date: date = '2026-10-28', 'Fallback date') "
            'lookup by_code { code = self.code::value(day=input.industry_date ?? input.as_of_date) }',
        ).replace(
            'lookup by_name { name = self.name::value() }',
            "@extra_input_param(day_text: string, 'Date text') "
            'lookup by_text { code = self.code::value(day=parse_date(input.day_text)) } '
            '@extra_input_param(day: date) '
            'lookup by_optional { code = self.code::value(day=input.day) }',
        )
        analysis = analyze([document(source, 'optional.yambl')])
        self.assertEqual(analysis.diagnostics, ())
        model = Lowering(analysis, Path('.'), '', 'test').model()
        from yambl_compiler.ir.codec import dumps, loads
        from yambl_compiler.ir.validation import validate_ir

        self.assertEqual(validate_ir(loads(dumps(model))), ())
        layout = build_layout(model)
        module = self.generated(model)
        docs = inspect.getdoc(module.SampleReader.prepare_security_by_code)
        self.assertIn('Optional date', docs)
        self.assertIn('Fallback date', docs)
        database = self.root / 'optional.sqlite'
        stored = layout.fields['core.Security.code']
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(ddl(layout))
            connection.execute(
                "INSERT INTO tb_core_security (id, entity_type) VALUES ('one', 'core.Security')"
            )
            connection.executemany(
                f'INSERT INTO {quote(stored.table)} (id, valid_from, valid_to, {quote(stored.state)}, {quote(stored.value)}) VALUES (?, ?, ?, ?, ?)',
                [
                    ('one', '2026-10-01', '2026-11-01', 'VALUE', 'October'),
                    ('one', '2026-11-01', '2026-12-01', 'VALUE', 'November'),
                ],
            )
            connection.commit()
        files = self.file_copy(layout, database)
        for opened in (module.SampleReader.open(database), module.SampleReader.open_files(files)):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            for arguments, expected in (
                ({}, 'October'),
                ({'as_of_date': date(2026, 11, 10)}, 'November'),
                ({'industry_date': date(2026, 11, 10)}, 'November'),
            ):
                prepared = reader.prepare_security_by_code(**arguments)
                self.assertIsInstance(prepared, Success)
                result = prepared.value.match_many([module.SecurityByCodeKey(code=expected)])
                self.assertEqual(result, Success((Success('one'),)))
            invalid = reader.prepare_security_by_code(industry_date='2026-11-10')
            self.assertEqual(invalid.status, Status.ERROR)
            text = reader.prepare_security_by_text(day_text='2026-10-28')
            self.assertIsInstance(text, Success)
            self.assertEqual(
                text.value.match_many([module.SecurityByTextKey(code='October')]),
                Success((Success('one'),)),
            )
            self.assertEqual(reader.prepare_security_by_text().status, Status.ERROR)
            self.assertEqual(reader.prepare_security_by_text(day_text='bad').status, Status.ERROR)
            rules = {r.name: r for r in layout.entities['core.Security'].lookups}
            for name, arguments in (
                ('by_code', {}),
                ('by_code', {'industry_date': date(2026, 11, 10)}),
                ('by_text', {'day_text': '2026-10-28'}),
                ('by_text', {}),
                ('by_optional', {}),
                ('by_code', {'unknown': 'unexpected'}),
            ):
                label = name.removeprefix('by_').capitalize()
                key = getattr(module, 'SecurityBy' + label + 'Key')
                matcher = getattr(module, 'SecurityBy' + label + 'Matcher')
                actual = prepare(
                    reader._session, 'core.Security', rules[name], key, str, arguments, matcher
                )
                if name in ('by_text', 'by_optional') and not arguments or 'unknown' in arguments:
                    self.assertEqual(actual.status, Status.ERROR)
                else:
                    self.assertIsInstance(actual, Success)
            reader.close()

    def test_lookup_expression_inputs_and_code_conversion_on_both_backends(self):
        source = """decodes Sources { - 1 Wind: 'Wind' - 2 Manual: 'Manual' }
const FALLBACK: string = 'Wind'
entity E {
    id id: string
    dimension codes { value source: code<Sources> }
    dimension strings { value source: string }
    @dimension(codes) attr code_value: string
    @dimension(strings) attr string_value: string
    @extra_input_param(flag: bool)
    lookup by_cond { value = self.code_value::value(source=if input.flag then 'Wind' else 'Manual') }
    @extra_input_param(choice: code<Sources>)
    lookup by_choice { value = self.string_value::value(source=to_string(input.choice)) }
    @extra_input_param(flag: bool)
    lookup by_constant { value = self.string_value::value(source=if input.flag then FALLBACK else Sources.Wind) }
    @extra_input_param(rate: float)
    @extra_input_param(moment: datetime)
    @extra_input_param(ref: E)
    lookup by_id { value = self.id::value() }
}
"""
        analysis = analyze([document(source, 'inputs.yambl')])
        self.assertEqual(analysis.diagnostics, ())
        model = Lowering(analysis, Path('.'), '', 'test').model()
        layout = build_layout(model)
        module = self.generated(model)
        database = self.root / 'inputs.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(ddl(layout))
            connection.execute("INSERT INTO tb_e (id, entity_type) VALUES ('one', 'E')")
            for field_name in ('code_value', 'string_value'):
                field = layout.fields['E.' + field_name]
                connection.executemany(
                    f'INSERT INTO {quote(field.table)} (id, dim_source, {quote(field.state)}, {quote(field.value)}) VALUES (?, ?, ?, ?)',
                    [('one', 'Wind', 'VALUE', 'wind'), ('one', 'Manual', 'VALUE', 'manual')],
                )
            connection.commit()
        files = self.file_copy(layout, database)
        for opened in (module.SampleReader.open(database), module.SampleReader.open_files(files)):
            self.assertIsInstance(opened, Success)
            reader = opened.value
            for flag, expected in ((True, 'wind'), (False, 'manual')):
                prepared = reader.prepare_e_by_cond(flag=flag)
                self.assertIsInstance(prepared, Success)
                self.assertEqual(
                    prepared.value.match_many([module.EByCondKey(value=expected)]),
                    Success((Success('one'),)),
                )
            self.assertEqual(reader.prepare_e_by_cond().status, Status.ERROR)
            choice = reader.prepare_e_by_choice(choice=module.SourcesCode.Wind)
            self.assertIsInstance(choice, Success)
            self.assertEqual(
                choice.value.match_many([module.EByChoiceKey(value='wind')]),
                Success((Success('one'),)),
            )
            self.assertEqual(reader.prepare_e_by_choice(choice='Wind').status, Status.ERROR)
            valid = reader.prepare_e_by_id(
                rate=1.25, moment=datetime.fromisoformat('2026-10-29T12:00:00+08:00')
            )
            self.assertIsInstance(valid, Success)
            self.assertEqual(reader.prepare_e_by_id(rate=float('nan')).status, Status.ERROR)
            self.assertEqual(
                reader.prepare_e_by_id(moment=datetime(2026, 10, 29)).status, Status.ERROR
            )
            rules = {r.name: r for r in layout.entities['E'].lookups}
            for name, arguments in (
                ('by_cond', {'flag': True}),
                ('by_cond', {}),
                ('by_choice', {'choice': module.SourcesCode.Wind}),
                ('by_constant', {'flag': True}),
                ('by_constant', {'flag': False}),
                (
                    'by_id',
                    {'rate': 1.25, 'moment': datetime.fromisoformat('2026-10-29T12:00:00+08:00')},
                ),
                ('by_id', {'ref': 'one'}),
            ):
                key = getattr(module, 'EBy' + name.removeprefix('by_').capitalize() + 'Key')
                matcher = getattr(module, 'EBy' + name.removeprefix('by_').capitalize() + 'Matcher')
                actual = prepare(reader._session, 'E', rules[name], key, str, arguments, matcher)
                if name == 'by_cond' and not arguments:
                    self.assertEqual(actual.status, Status.ERROR)
                else:
                    self.assertIsInstance(actual, Success)
            reader.close()

    def test_real_sqlite_prepare_match_and_closed_reader(self):
        module = self.generated()
        path = self.root / 'lookup.sqlite'
        stored = self.layout.fields['core.Security.name']
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(ddl(self.layout))
            connection.execute(
                f'INSERT INTO {quote(table_name("core.Security"))} (id, entity_type, {quote(stored.state)}, {quote(stored.value)}) VALUES (?, ?, ?, ?)',
                ('one', 'core.Security', 'VALUE', 'Alpha'),
            )
            connection.execute(
                f'INSERT INTO {quote(table_name("core.Security"))} (id, entity_type, {quote(stored.state)}, {quote(stored.value)}) VALUES (?, ?, ?, ?)',
                ('two', 'core.Bond', 'VALUE', 'Alpha'),
            )
            scd = self.layout.fields['core.Security.code']
            connection.execute(
                f'INSERT INTO {quote(scd.table)} (id, valid_from, valid_to, {quote(scd.state)}, {quote(scd.value)}) VALUES (?, ?, ?, ?, ?)',
                ('two', '2026-10-01', '2026-11-01', 'VALUE', 'X'),
            )
            connection.commit()
            connection.row_factory = sqlite3.Row
            tables = {
                table.name: [
                    dict(row) for row in connection.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in self.layout.tables
            }
        files = self.root / 'files'
        for name, content in repository_contents(self.layout, tables).items():
            target = files / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
        opened = module.SampleReader.open(path)
        self.assertIsInstance(opened, Success)
        reader = opened.value
        prepared = reader.prepare_security_by_name()
        self.assertIsInstance(prepared, Success)
        self.assertIsInstance(prepared.value, module.SecurityByNameMatcher)
        child = reader.prepare_bond_by_name()
        self.assertIsInstance(child, Success)
        self.assertIsInstance(child.value, module.SecurityByNameMatcher)
        code = reader.prepare_security_by_code(as_of_date=date(2026, 10, 28))
        self.assertIsInstance(code, Success)
        self.assertEqual(
            code.value.match_many([module.SecurityByCodeKey(code='X')]).value, (Success('two'),)
        )
        invalid = reader.prepare_security_by_code(as_of_date='2026-10-28')
        self.assertIsInstance(invalid, module.NonNormalResult)
        self.assertEqual(invalid.status, Status.ERROR)
        reader.close()
        batch = prepared.value.match_many(
            [module.SecurityByNameKey(name='Alpha'), module.SecurityByNameKey(name='missing')]
        )
        self.assertIsInstance(batch, Success)
        self.assertEqual(batch.value[0].candidates, ('one', 'two'))
        self.assertEqual(batch.value[1].status, Status.NO_ENTRY)
        self.assertEqual(
            child.value.match_many([module.SecurityByNameKey(name='Alpha')]).value,
            (Success('two'),),
        )
        file_opened = module.SampleReader.open_files(files)
        self.assertIsInstance(file_opened, Success)
        file_reader = file_opened.value
        file_prepared = file_reader.prepare_security_by_name()
        self.assertIsInstance(file_prepared, Success)
        file_reader.close()
        file_batch = file_prepared.value.match_many([module.SecurityByNameKey(name='Alpha')])
        self.assertIsInstance(file_batch, Success)
        self.assertEqual(file_batch.value[0].candidates, ('one', 'two'))
        with self.assertRaises(TypeError):
            reader.prepare_security_by_code(date(2026, 10, 28))
        script = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sample_model import SampleReader, SecurityByNameKey, Success
matches = []
for factory, name in ((SampleReader.open, sys.argv[2]), (SampleReader.open_files, sys.argv[3])):
    opened = factory(Path(name))
    assert isinstance(opened, Success), opened
    reader = opened.value
    prepared = reader.prepare_security_by_name()
    assert isinstance(prepared, Success), prepared
    reader.close()
    results = prepared.value.match_many([SecurityByNameKey(name='Alpha')])
    assert isinstance(results, Success), results
    matches.append(list(results.value[0].candidates))
print(json.dumps(matches))
'''
        result = subprocess.run(
            [
                sys.executable,
                '-I',
                '-X',
                'utf8',
                '-c',
                script,
                str(self.root / 'generated'),
                str(path),
                str(files),
            ],
            capture_output=True,
            encoding='utf-8',
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [['one', 'two'], ['one', 'two']])
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                f'UPDATE {quote(table_name("core.Security"))} SET {quote(stored.value)} = ? WHERE id = ?',
                ('Beta', 'two'),
            )
            connection.execute(
                f'INSERT INTO {quote(scd.table)} (id, valid_from, valid_to, {quote(scd.state)}, {quote(scd.value)}) VALUES (?, ?, ?, ?, ?)',
                ('two', '2026-10-15', '2026-12-01', 'VALUE', 'Y'),
            )
            connection.commit()
            connection.row_factory = sqlite3.Row
            changed_tables = {
                table.name: [
                    dict(row) for row in connection.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in self.layout.tables
            }
        for name, content in repository_contents(self.layout, changed_tables).items():
            (files / name).write_text(content, encoding='utf-8', newline='\n')
        for opened in (module.SampleReader.open(path), module.SampleReader.open_files(files)):
            self.assertIsInstance(opened, Success)
            current = opened.value
            new = current.prepare_security_by_name()
            self.assertIsInstance(new, Success)
            failure = current.prepare_security_by_code(as_of_date=date(2026, 10, 28))
            self.assertIsInstance(failure, module.NonNormalResult)
            self.assertEqual(failure.status, Status.ERROR)
            self.assertIn('key code', failure.message)
            self.assertIn('two', failure.message)
            current.close()
            self.assertEqual(
                new.value.match_many([module.SecurityByNameKey(name='Beta')]).value,
                (Success('two'),),
            )
        self.assertEqual(
            prepared.value.match_many([module.SecurityByNameKey(name='Alpha')]).value[0].candidates,
            ('one', 'two'),
        )
        self.assertEqual(
            file_prepared.value.match_many([module.SecurityByNameKey(name='Alpha')])
            .value[0]
            .candidates,
            ('one', 'two'),
        )
