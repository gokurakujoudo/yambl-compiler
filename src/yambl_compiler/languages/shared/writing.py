"""Render exact data literals and coordinate records."""

from decimal import Decimal

from .model import Condition, ObjectValue, Symbol, Value


def literal(value: Value) -> str:
    """Render an exact, single-line YAMBL data literal.

    :param value: Typed literal.
    :returns: Canonical spelling.
    :raises ValueError: On multiline strings or nonfinite numbers.
    """
    if isinstance(value, str):
        if '\n' in value or '\r' in value:
            raise ValueError('strings must fit on one physical line')
        return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('numbers must be finite')
        text = format(value, 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.')
        if value == 0:
            text = '0'
        return text + '.0' if '.' not in text else text
    if isinstance(value, Symbol):
        return value.name
    if isinstance(value, ObjectValue):
        return '{' + ', '.join(literal(k) + ': ' + literal(v) for k, v in value.items) + '}'
    if isinstance(value, tuple):
        return '[' + ', '.join(literal(v) for v in value) + ']'
    return str(value)


def scope_text(conditions: tuple[Condition, ...]) -> str:
    """Render a structured scope.

    :param conditions: Ordered coordinate conditions.
    :returns: Scope clause.
    """
    return (
        'scope ('
        + ', '.join(
            c.name
            + (
                '=' + literal(c.value)
                if c.end is None
                else ' in [' + literal(c.value) + ', ' + literal(c.end) + ')'
            )
            for c in conditions
        )
        + ')'
    )


def data_lines(
    header: str, columns: tuple[str, ...], rows: tuple[tuple[Value, ...], ...], depth: int
) -> list[str]:
    """Render one table of business records.

    :param header: Text preceding the column list.
    :param columns: Ordered columns.
    :param rows: Records matching the columns.
    :param depth: Four-space indentation level.
    :returns: Complete data block lines.
    """
    indent = '    ' * depth
    return [
        indent + header + ' (' + ', '.join(columns) + ') {',
        *(indent + '    ' + ', '.join(literal(v) for v in row) for row in rows),
        indent + '}',
    ]
