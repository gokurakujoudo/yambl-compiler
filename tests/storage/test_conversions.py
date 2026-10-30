"""Check explicit scalar conversion boundaries independently of storage."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from yambl_compiler.storage.arithmetic import lift
from yambl_compiler.storage.conversions import convert
from yambl_compiler.storage.results import EntityRef, NonNormalResult, Status, Success


class ConversionTests(unittest.TestCase):
    """Exercise exact conversion, explicit rounding and ISO time handling."""

    def test_values(self) -> None:
        """Convert supported scalars and verify exact results.

        :returns: None.
        """
        cases: list[tuple[str, tuple[object, ...], object]] = [
            ('to_integer', ('1.00',), 1),
            ('to_integer', (2.0,), 2),
            ('to_decimal', ('1.25',), Decimal('1.25')),
            ('to_decimal', (0.1,), Decimal(0.1)),
            ('to_decimal', (123,), Decimal(123)),
            ('to_float', (Decimal('1.25'),), 1.25),
            ('to_string', (True,), 'true'),
            ('to_string', (False,), 'false'),
            ('to_string', (2,), '2'),
            ('to_string', (1.25,), '1.25'),
            ('to_string', (Decimal('1.25'),), '1.25'),
            ('to_string', ('a',), 'a'),
            ('to_string', (EntityRef('E', 1),), '1'),
            ('to_string', (date(2026, 1, 1),), '2026-01-01'),
            ('parse_date', ('2026-01-01',), date(2026, 1, 1)),
            ('format_date', (date(2026, 1, 1),), '2026-01-01'),
            (
                'parse_datetime',
                ('2026-01-01T08:00:00+08:00',),
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            (
                'format_datetime',
                (datetime(2026, 1, 1, tzinfo=timezone.utc),),
                '2026-01-01T00:00:00Z',
            ),
            ('to_string', (datetime(2026, 1, 1, tzinfo=timezone.utc),), '2026-01-01T00:00:00Z'),
            ('truncate', (Decimal('-1.239'), 2), Decimal('-1.23')),
            ('truncate', (1234, -2), Decimal('1200')),
            ('round', (Decimal('1.25'), 1, 'half_even'), Decimal('1.2')),
            ('round', (Decimal('1.25'), 1, 'half_up'), Decimal('1.3')),
            ('round', (Decimal('-1.21'), 1, 'floor'), Decimal('-1.3')),
            ('round', (Decimal('-1.21'), 1, 'ceil'), Decimal('-1.2')),
            ('round', (Decimal('-1.21'), 1, 'toward_zero'), Decimal('-1.2')),
        ]
        with localcontext() as context:
            context.prec = 2
            for name, arguments, expected in cases:
                with self.subTest(name=name, arguments=arguments):
                    self.assertEqual(convert(name, arguments), expected)
            self.assertEqual(
                convert('truncate', (Decimal('123456789.123'), 2)), Decimal('123456789.12')
            )

    def test_invalid(self) -> None:
        """Reject invalid, lossy, ambiguous and nonfinite inputs.

        :returns: None.
        """
        cases: list[tuple[str, tuple[object, ...]]] = [
            ('unknown', (1,)),
            ('to_integer', ()),
            ('to_integer', ('1.5',)),
            ('to_decimal', (True,)),
            ('to_decimal', (object(),)),
            ('to_decimal', (' 1',)),
            ('to_decimal', ('1_000',)),
            ('to_decimal', ('NaN',)),
            ('to_decimal', (float('inf'),)),
            ('to_decimal', (Decimal('NaN'),)),
            ('to_float', ('0.1',)),
            ('to_float', ('1e999',)),
            ('to_string', ([],)),
            ('parse_date', ('20260101',)),
            ('parse_date', (1,)),
            ('parse_date', ('2026-02-30',)),
            ('format_date', (datetime(2026, 1, 1),)),
            ('parse_datetime', ('2026-01-01T00:00:00',)),
            ('parse_datetime', (1,)),
            ('parse_datetime', ('2026-02-30T00:00:00Z',)),
            ('parse_datetime', ('0001-01-01T00:00:00+01:00',)),
            ('parse_datetime', ('9999-12-31T23:59:59-01:00',)),
            ('parse_datetime', ('2026-01-01T00:00:00.1234567Z',)),
            ('format_datetime', (datetime(2026, 1, 1),)),
            ('format_datetime', ('2026-01-01T00:00:00Z',)),
            ('truncate', (1, True)),
            ('round', (1, 2, 'unknown')),
            ('round', (1, 2, 1)),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                result = lift(lambda: convert(name, arguments), [])
                self.assertIsInstance(result, NonNormalResult)
                if isinstance(result, NonNormalResult):
                    self.assertEqual(result.status, Status.ERROR)
        self.assertEqual(
            lift(lambda x: convert('to_integer', (x,)), [Success('2')]), Success(2, 'computation')
        )
