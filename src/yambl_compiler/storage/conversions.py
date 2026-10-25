"""Pure, explicit scalar conversions shared by compilation and generated code."""

import math
import re
from datetime import date, datetime, timezone
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    localcontext,
)
from typing import cast

from yambl_compiler.storage.results import EntityRef

SIGNATURES: dict[str, tuple[tuple[str, str], ...]] = {
    'to_string': (('value', 'scalar'),),
    'to_integer': (('value', 'number_or_string'),),
    'to_decimal': (('value', 'number_or_string'),),
    'to_float': (('value', 'number_or_string'),),
    'parse_date': (('text', 'string'),),
    'format_date': (('value', 'date'),),
    'parse_datetime': (('text', 'string'),),
    'format_datetime': (('value', 'datetime'),),
    'truncate': (('value', 'number'), ('scale', 'integer')),
    'round': (('value', 'number'), ('scale', 'integer'), ('mode', 'string')),
}
RETURNS = {
    'to_string': 'string',
    'to_integer': 'integer',
    'to_decimal': 'decimal',
    'to_float': 'float',
    'parse_date': 'date',
    'format_date': 'string',
    'parse_datetime': 'datetime',
    'format_datetime': 'string',
    'truncate': 'decimal',
    'round': 'decimal',
}
ROUNDING = {
    'half_even': ROUND_HALF_EVEN,
    'half_up': ROUND_HALF_UP,
    'floor': ROUND_FLOOR,
    'ceil': ROUND_CEILING,
    'toward_zero': ROUND_DOWN,
}


def decimal_value(value: object) -> Decimal:
    """Convert a numeric scalar without losing information.

    :param value: Integer, finite decimal, float or decimal text.
    :returns: Exact decimal representation, including the full binary float value.
    :raises ValueError: For invalid text, bool, unsupported or nonfinite values.
    """
    if type(value) not in (int, float, Decimal, str):
        raise ValueError('conversion requires a number or numeric string')
    if isinstance(value, str):
        if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', value):
            raise ValueError('invalid decimal text')
        result = Decimal(value)
    else:
        result = Decimal(cast(int | float | Decimal, value))
    if not result.is_finite():
        raise ValueError('nonfinite numeric conversion')
    return result


def convert(name: str, arguments: tuple[object, ...]) -> object:
    """Execute a validated pure builtin, without querying data or ambient settings.

    :param name: Builtin identity.
    :param arguments: Values in formal parameter order.
    :returns: Converted scalar.
    :raises ValueError: For invalid arguments or lossy ordinary conversions.
    :raises OverflowError: When a value cannot be represented.
    """
    signature = SIGNATURES.get(name)
    if signature is None or len(arguments) != len(signature):
        raise ValueError('unknown conversion or incorrect argument count')
    value = arguments[0]
    if name == 'to_string':
        if isinstance(value, EntityRef):
            return str(value.id)
        if type(value) not in (str, bool, int, float, Decimal, date, datetime):
            raise ValueError('to_string requires a scalar value')
        if isinstance(value, datetime):
            return convert('format_datetime', (value,))
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, (float, Decimal)):
            decimal_value(value)
        return str(value)
    if name == 'parse_date':
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError('parse_date requires YYYY-MM-DD')
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError('invalid ISO calendar date') from error
    if name == 'format_date':
        if type(value) is not date:
            raise ValueError('format_date requires date')
        return value.isoformat()
    if name in ('parse_datetime', 'format_datetime'):
        if name == 'parse_datetime':
            if not isinstance(value, str) or not re.fullmatch(
                r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                r'(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})',
                value,
            ):
                raise ValueError('parse_datetime requires ISO datetime with explicit timezone')
            try:
                value = datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError('invalid ISO datetime') from error
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError('datetime requires an explicit timezone')
        utc = value.astimezone(timezone.utc)
        return utc if name == 'parse_datetime' else utc.isoformat().replace('+00:00', 'Z')
    numeric = decimal_value(value)
    if name == 'to_integer':
        result = int(numeric)
        if numeric != result:
            raise ValueError('to_integer does not truncate; use truncate or round explicitly')
        return result
    if name == 'to_decimal':
        return numeric
    if name == 'to_float':
        floating = float(numeric)
        if not math.isfinite(floating) or Decimal(floating) != numeric:
            raise ValueError('to_float cannot represent this value exactly')
        return floating
    scale = arguments[1]
    mode = 'toward_zero' if name == 'truncate' else arguments[2]
    if type(scale) is not int or not isinstance(mode, str) or mode not in ROUNDING:
        raise ValueError('scale must be integer and mode must be an explicit rounding mode')
    quantum = Decimal((0, (1,), -scale))
    with localcontext() as context:
        context.prec = max(len(numeric.as_tuple().digits), numeric.adjusted() + scale + 1, 1) + 1
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        return numeric.quantize(quantum, rounding=ROUNDING[mode])
