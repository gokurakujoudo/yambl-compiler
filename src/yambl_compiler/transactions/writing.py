"""Render transaction documents and compute whitespace-independent integrity."""

import hashlib
from dataclasses import replace
from decimal import Decimal

from yambl_compiler.transactions.model import (
    Condition,
    ObjectValue,
    Operation,
    Plan,
    Symbol,
    Transaction,
    Value,
)


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


def operation_lines(operation: Operation, depth: int) -> list[str]:
    """Render a mutation without source locations.

    :param operation: Parsed operation.
    :param depth: Indentation level.
    :returns: Operation lines.
    """
    header = operation.kind + ' ' + operation.target
    if operation.owned_data:
        header += ' owned_data delete_all'
    if operation.scope:
        header += ' ' + scope_text(operation.scope)
        if operation.kind == 'delete':
            return ['    ' * depth + header]
        header += ' columns'
    return data_lines(header, operation.columns, operation.rows, depth)


def plan_lines(plan: Plan, depth: int = 0) -> list[str]:
    """Render a standalone or embedded Plan.

    :param plan: Plan data.
    :param depth: Indentation level.
    :returns: Source lines.
    """
    indent = '    ' * depth
    lines = [
        indent + 'plan ' + plan.name + ' {',
        indent + '    version ' + str(plan.version),
        indent + '    model ' + literal(plan.model),
    ]
    for group in plan.groups:
        lines.append(indent + '    group ' + group.name + ' {')
        for operation in group.operations:
            lines.extend(operation_lines(operation, depth + 2))
        lines.append(indent + '    }')
    lines.append(indent + '}')
    return lines


def transaction_lines(transaction: Transaction, digest: str) -> list[str]:
    """Render an execution record with the supplied digest.

    :param transaction: Execution data.
    :param digest: Digest to write in the envelope.
    :returns: Transaction lines.
    """
    lines = [
        'transaction ' + literal(transaction.id) + ' {',
        '    version ' + str(transaction.version),
        '    time ' + literal(transaction.time),
    ]
    if transaction.source:
        lines.append('    source ' + literal(transaction.source))
    lines.append('    digest ' + literal(digest))
    lines.extend(plan_lines(transaction.plan, 1))
    for group in transaction.groups:
        lines.append('    group ' + group.name + ' {')
        for change in group.changes:
            lines.append('        change ' + change.target + ' ' + scope_text(change.scope) + ' {')
            for label, images in (('before', change.before), ('after', change.after)):
                for image in images:
                    lines.extend(
                        data_lines(label + ' ' + image.target, image.columns, image.rows, 3)
                    )
            lines.append('        }')
        lines.extend('        warning ' + literal(w) for w in group.warnings)
        lines.append('    }')
    lines.append('}')
    return lines


def semantic_digest(transaction: Transaction) -> str:
    """Hash canonical data, excluding the digest itself and source formatting.

    :param transaction: Execution record.
    :returns: Lowercase SHA-256.
    """
    return hashlib.sha256(
        ('\n'.join(transaction_lines(transaction, '')) + '\n').encode()
    ).hexdigest()


def seal(transaction: Transaction) -> Transaction:
    """Fill an execution record's integrity digest.

    :param transaction: Record before publication.
    :returns: Immutable sealed record.
    """
    return replace(transaction, digest=semantic_digest(transaction))


def dumps(document: Plan | Transaction) -> str:
    """Serialize and validate one document before publication.

    :param document: Plan or execution record.
    :returns: UTF-8-ready text with one final LF.
    :raises ValueError: On an invalid document or stale transaction digest.
    """
    from yambl_compiler.transactions.syntax import parse

    lines = (
        plan_lines(document)
        if isinstance(document, Plan)
        else transaction_lines(document, document.digest)
    )
    text = '\n'.join(lines) + '\n'
    if parse(text) != document:
        raise ValueError('document does not round-trip through the data grammar')
    return text
