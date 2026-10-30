"""Detached scalar rules use the same input and decoding contracts as readers."""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from tests.support import document
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.model import Text, Type, Value
from yambl_compiler.languages.yambl.computation.records import ValueField
from yambl_compiler.languages.yambl.semantics.validation import analyze
from yambl_compiler.storage.results import EntityRef
from yambl_compiler.storage.scalars import ScalarRule, decode_scalar, encode_input, finite_matches


class Code(StrEnum):
    One = 'One'


class OtherCode(StrEnum):
    One = 'One'


def scalar(name, *, kind='value', target='', arguments=(), parameters=()):
    return Type(name, kind, target, arguments, parameters, None, False, (), ())


class ScalarRuleTests(unittest.TestCase):
    def test_finite_values_compare_typed_payloads(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Point:
            amount: Decimal

        point = Value(
            'structured', '', 'Point', fields=(ValueField('amount', Value('decimal', '1.20', '')),)
        )
        cases = (
            (point, Point(Decimal('1.2')), True),
            (point, Point(Decimal('2')), False),
            (Value('reference', '5', 'A'), EntityRef[object]('A', 5), True),
            (Value('float', '1.5', ''), 1.5, True),
            (Value('date', '2026-01-01', ''), date(2026, 1, 1), True),
            (Value('date', '2026-01-01', ''), date(2026, 1, 2), False),
        )
        for member, value, expected in cases:
            with self.subTest(member=member, value=value):
                self.assertEqual(finite_matches(member, value), expected)

    def test_datetime_storage(self):
        instant = scalar('datetime')
        utc = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
        offset = datetime(2025, 12, 31, 19, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(encode_input(instant, offset), '2026-01-01T00:00:00.123456Z')
        self.assertEqual(decode_scalar(instant, '2026-01-01T00:00:00.123456Z'), utc)
        for value in (datetime(2026, 1, 1), date(2026, 1, 1), '2026-01-01T00:00:00Z'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                encode_input(instant, value)
        for value in (
            '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00.1Z',
            '2026-02-30T00:00:00Z',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_scalar(instant, value)

    def test_dictionary_singleton_reference_from_real_model(self):
        source = 'decodes D {} entity Base { id id:string attr target:Decodes } entity Child inherits Base { @override attr target:exact<D> }'
        analysis = analyze([document(source)])
        self.assertEqual(analysis.diagnostics, ())
        model = Lowering(analysis, Path('.'), '', 'test').model()
        child = next(item for item in model.definitions if item.identity == 'Child')
        reference_type = next(field.type for field in child.fields if field.name == 'target')
        decodes = next(item for item in model.definitions if item.identity == 'builtins.Decodes')
        id_type = next(field.type for field in decodes.fields if field.category == 'id')
        rule = ScalarRule(reference_type, reference_id=id_type)
        self.assertEqual(
            rule.check(EntityRef[object]('builtins.Decodes', 'D')),
            EntityRef[object]('builtins.Decodes', 'D'),
        )
        with self.assertRaisesRegex(ValueError, 'reference violates exact constraint'):
            rule.check(EntityRef[object]('builtins.Decodes', 'Other'))

    def test_strict_types_constraints_and_dictionary_identity(self):
        integer = ScalarRule(scalar('integer'))
        with self.assertRaises(ValueError):
            integer.check(True)
        self.assertEqual(integer.check(3), 3)
        day = ScalarRule(scalar('date'))
        with self.assertRaises(ValueError):
            day.check('2026-10-29')
        self.assertEqual(day.check(date(2026, 10, 29)), date(2026, 10, 29))
        money = ScalarRule(
            scalar('decimal', parameters=(Text('precision', '4'), Text('scale', '2')))
        )
        self.assertEqual(money.check(Decimal('1.20')), Decimal('1.2'))
        with self.assertRaises(ValueError):
            money.check(Decimal('123.45'))
        dictionary = scalar('D', target='d.D')
        code = ScalarRule(scalar('code', arguments=(dictionary,)), Code, frozenset({'One'}))
        self.assertIs(code.check(Code.One), Code.One)
        with self.assertRaises(ValueError):
            code.check(OtherCode.One)
        with self.assertRaises(ValueError):
            code.check('One')
        reference = ScalarRule(
            scalar('Security', kind='reference', target='core.Security'),
            reference_id=scalar('integer'),
        )
        self.assertEqual(
            reference.check(EntityRef[object]('core.Security', 5)),
            EntityRef[object]('core.Security', 5),
        )
        with self.assertRaises(ValueError):
            reference.check(EntityRef[object]('core.Issuer', 5))


if __name__ == '__main__':
    unittest.main()
