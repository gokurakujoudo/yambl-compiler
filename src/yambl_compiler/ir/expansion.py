"""Expand transparent aliases in semantic expressions without losing dependencies."""

from dataclasses import replace
from typing import cast

from yambl_compiler.ir.model import Definition, Expression, Operation


def expand(
    expression: Expression,
    aliases: dict[str, Definition],
    bindings: dict[str, Expression] | None = None,
) -> Expression:
    """Substitute checked aliases and their lexical formal bindings.

    :param expression: Resolved expression from a successfully checked model.
    :param aliases: Alias definitions keyed by portable identity.
    :param bindings: Arguments supplied to the current alias.
    :returns: Expanded expression retaining self, selections and mapping bindings.
    """
    scope = dict(bindings or {})
    arguments: list[Expression] = []
    for argument in expression.arguments:
        arguments.append(expand(argument, aliases, scope))
        if argument.binding:
            scope.pop(argument.binding, None)
    result = replace(
        expression,
        arguments=tuple(arguments),
        operations=tuple(
            Operation(op.operator, expand(op.operand, aliases, scope))
            for op in expression.operations
        ),
    )
    if result.kind == 'parameter' and result.name in scope:
        supplied = scope[result.name]
        result = replace(
            supplied,
            accesses=supplied.accesses + result.accesses,
            operations=supplied.operations + result.operations,
            binding=result.binding or supplied.binding,
        )
    elif result.kind == 'reference' and result.target in aliases:
        alias = aliases[result.target]
        body = expand(
            cast(Expression, alias.expression),
            aliases,
            {p.name: arg for p, arg in zip(alias.parameters, arguments, strict=True)},
        )
        result = replace(
            body,
            accesses=body.accesses + result.accesses,
            operations=body.operations + result.operations,
            binding=result.binding or body.binding,
        )
    return result
