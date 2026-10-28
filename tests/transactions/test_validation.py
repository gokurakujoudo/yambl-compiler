"""Check Plan names and literal types without business repository data."""

from pathlib import Path

from tests.transactions.test_binding import CatalogCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.shared.model import Condition, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.transactions.validation import PlanValidator


class ValidationTests(CatalogCase):
    def test_model_validation_is_independent_of_instance_existence(self):
        validator = PlanValidator(self.catalog.layout)
        scope = (Condition('key', 'missing'),)
        for op in (
            Operation('insert', 'A', ('key', 'text', 'members'), (('absent', 'name', (1, 2)),)),
            Operation('update', 'A', ('key', 'text'), (('absent', Symbol('EMPTY')),)),
            Operation('delete', 'A', ('key',), (('absent',),)),
            Operation('delete', 'A.price', (), (), scope, expect='any'),
            Operation(
                'replace', 'A.price', ('day', 'source', 'value'), (('2026-01-01', 'One', 2),), scope
            ),
            Operation('replace', 'A.text', ('value',), (('name',),), scope),
        ):
            with self.subTest(op=op):
                validator.validate(
                    Plan('p', self.catalog.layout.model.model_digest, (Group('g', (op,)),))
                )
        self.assertEqual(validator.data.tables, {})
        with self.assertRaisesRegex(ValueError, 'digest'):
            validator.validate(Plan('p', '0' * 64, ()))

    def test_invalid_names_types_and_empty_headers(self):
        validator = PlanValidator(self.catalog.layout)
        scope = (Condition('key', 'missing'),)
        cases = (
            (Operation('insert', 'Unknown', ('key',), ()), 'unknown entity'),
            (Operation('insert', 'A', ('key', 'unknown'), ()), 'not writable'),
            (Operation('replace', 'A', ('key',), ()), 'field target'),
            (Operation('insert', 'A', ('text',), ()), 'ID column'),
            (Operation('insert', 'A', ('key',), ((1,),)), 'literal'),
            (Operation('insert', 'A', ('key', 'count'), (('a', 'wrong'),)), 'integer'),
            (Operation('delete', 'A', ('key', 'text'), ()), 'not writable'),
            (Operation('insert', 'A', ('key', 'price'), ()), 'scope'),
            (Operation('update', 'A', ('key', 'members'), ()), 'scope'),
            (Operation('insert', 'A', ('key', 'members'), (('a', (1, 1)),)), 'duplicate'),
            (Operation('insert', 'A', ('key', 'members'), (('a', ('bad',)),)), 'integer'),
            (Operation('insert', 'A', ('key', 'members'), (('a', 'bad'),)), 'collection'),
            (Operation('update', 'A.text', ('value',), ()), 'scope'),
            (Operation('replace', 'A.text', ('bad',), (), scope), 'columns'),
            (
                Operation(
                    'replace', 'A.price', ('day', 'source', 'value'), ((1, 'One', 2),), scope
                ),
                'ISO',
            ),
            (
                Operation(
                    'replace',
                    'A.price',
                    ('day', 'source', 'value'),
                    (('20260101', 'One', 2),),
                    scope,
                ),
                'ISO',
            ),
            (Operation('delete', 'A.text', (), (), (Condition('unknown', 1),)), 'coordinate'),
        )
        for op, message in cases:
            with self.subTest(op=op), self.assertRaisesRegex(ValueError, message):
                validator.operation(op)

    def test_plan_write_independence_and_optional_digest(self):
        validator = PlanValidator(self.catalog.layout)
        scope = (Condition('key', 'a'),)
        insert = Operation('insert', 'A', ('key', 'text'), (('a', 'first'),))
        update = Operation('update', 'A', ('key', 'count'), (('a', 1),))
        first = Operation('replace', 'A.price', ('day', 'source', 'value'), (), scope)
        second = Operation('delete', 'A.price', (), (), (*scope, Condition('source', 'One')))
        repeated = Operation('update', 'A', ('key', 'text'), (('a', 'second'),))
        for operations in ((insert, repeated), (first, second)):
            with (
                self.subTest(operations=operations),
                self.assertRaisesRegex(ValueError, 'overlapping'),
            ):
                validator.validate(
                    Plan('p', None, tuple(Group(str(i), (op,)) for i, op in enumerate(operations)))
                )
        validator.validate(Plan('p', None, (Group('g', (insert,)),)))
        validator.validate(Plan('p', None, (Group('g', (insert, update, first)),)))
        validator.validate(Plan('p', None, (Group('g', (update, first)),)))
        validator.validate(
            Plan(
                'p',
                None,
                (Group('g', (insert, Operation('update', 'A', ('key', 'count'), (('b', 1),)))),),
            )
        )

    def test_replacement_coordinates_must_be_within_scope(self):
        validator = PlanValidator(self.catalog.layout)
        scope = (Condition('key', 'a'),)
        period = Condition('day', '2026-01-01', '2026-02-01')
        cases = (
            (Operation('replace', 'A.text', ('key', 'value'), (('b', 'name'),), scope), 'outside'),
            (
                Operation(
                    'replace',
                    'A.price',
                    ('day', 'source', 'value'),
                    (('2026-01-02', 'Two', 1),),
                    (*scope, Condition('source', 'One')),
                ),
                'outside',
            ),
            (
                Operation(
                    'replace',
                    'A.price',
                    ('day', 'source', 'value'),
                    (('2026-02-01', 'One', 1),),
                    (*scope, period),
                ),
                'outside',
            ),
            (
                Operation(
                    'replace',
                    'A.outstanding',
                    ('valid_from', 'valid_to', 'value'),
                    (('2026-01-01', '2026-02-02', 1),),
                    (*scope, period),
                ),
                'crosses',
            ),
            (
                Operation(
                    'replace',
                    'A.outstanding',
                    ('valid_from', 'valid_to', 'value'),
                    (('2026-01-02', '2026-01-02', 1),),
                    (*scope, period),
                ),
                'crosses',
            ),
        )
        for operation, message in cases:
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, message):
                validator.validate(Plan('p', None, (Group('g', (operation,)),)))
        valid = Operation(
            'replace',
            'A.outstanding',
            ('valid_from', 'valid_to', 'value'),
            (('2026-01-01', '2026-02-01', 1),),
            (*scope, period),
        )
        validator.validate(Plan('p', None, (Group('g', (valid,)),)))

    def test_fmc_plan_including_dynamic_decodes_matches_compiled_contract(self):
        repository = Path(__file__).parents[3] / 'fmc-data-model'
        plan = parse((repository / 'sample_data/demo.yaplan').read_text(encoding='utf-8'))
        PlanValidator(build_layout(compile_repository(repository / 'model'))).validate(plan)
        group = next(g for g in plan.groups if g.name == 'builtins_Decodes')
        self.assertTrue(
            {'PortfolioGroupTypes', 'TradingMarkets', 'PriceSources', 'ExternalRatingSources'}
            <= {row[0] for row in group.operations[0].rows}
        )
