"""Render grouped data plans."""

from ..shared.writing import data_lines, literal, scope_text
from .model import Operation, Plan


def operation_lines(operation: Operation, depth: int) -> list[str]:
    """Render a mutation without source locations.

    :param operation: Parsed operation.
    :param depth: Indentation level.
    :returns: Operation lines.
    """
    header = operation.kind + ' ' + operation.target
    if operation.expect:
        header += ' expect ' + operation.expect
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
