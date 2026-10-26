"""Evaluate pure constant expressions without source execution or data reads."""

import math
import operator
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from yambl_compiler.languages.yambl.computation.records import Expression
from yambl_compiler.storage.arithmetic import Number, numeric
from yambl_compiler.storage.conversions import SIGNATURES, convert


@dataclass(frozen=True)
class StructuredConstant:
    """Retain the hashable identity and fields of a compile-time value.

    :param target: Structured value type identity.
    :param fields: Named immutable field payloads.
    """

    target: str
    fields: tuple[tuple[str, object], ...]


def evaluate(expression: Expression, resolve: Callable[[str], object]) -> object:
    """Evaluate an already typed pure expression.

    :param expression: Portable expression tree.
    :param resolve: Resolver for constant names and dictionary codes.
    :returns: Immutable scalar result.
    :raises ValueError: For operations requiring runtime data or unsupported syntax.
    """
    node = expression

    def child(value: Expression) -> object:
        """Evaluate one child in the same scope.

        :param value: Child expression.
        :returns: Constant value.
        """
        return evaluate(value, resolve)

    if node.kind == 'sequence':
        return frozenset(child(value) for value in node.operands)
    if node.kind == 'object':
        pairs = [
            (
                (
                    key.value
                    if key.kind == 'name' and node.annotation and node.annotation.name != 'mapping'
                    else child(key)
                ),
                child(value),
            )
            for key, value in zip(node.operands[::2], node.operands[1::2], strict=True)
        ]
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('duplicate mapping literal key')
        if node.annotation and node.annotation.target:
            return StructuredConstant(
                node.annotation.target, tuple(sorted((str(k), v) for k, v in result.items()))
            )
        return MappingProxyType(result)
    if node.annotation and node.kind in ('string', 'integer', 'decimal'):
        if node.annotation.name in ('date', 'datetime'):
            return convert('parse_' + node.annotation.name, (node.value,))
        if node.annotation.name in ('float', 'decimal'):
            if node.annotation.name == 'float':
                result_float = float(node.value)
                if not math.isfinite(result_float):
                    raise ValueError('nonfinite floating-point calculation')
                return result_float
            return Decimal(node.value)
    if node.kind == 'string':
        return node.value
    if node.kind == 'integer':
        return int(node.value)
    if node.kind == 'decimal':
        return Decimal(node.value)
    if node.kind == 'bool':
        return node.value == 'true'
    if node.kind == 'name':
        return resolve(node.value)
    if node.kind == 'member' and node.operands[0].kind == 'name':
        return resolve(node.operands[0].value + '.' + node.value)
    if node.kind == 'conditional':
        return child(node.operands[1] if child(node.operands[0]) else node.operands[2])
    if node.kind == 'unary':
        value = child(node.operands[0])
        return not value if node.value == 'not' else numeric('-', 0, cast(Number, value))
    if node.kind == 'binary':
        left = child(node.operands[0])
        if node.value == 'and':
            return left and child(node.operands[1])
        if node.value == 'or':
            return left or child(node.operands[1])
        right = child(node.operands[1])
        if node.value in ('+', '-', '*', '/', '//'):
            return numeric(node.value, cast(Number, left), cast(Number, right))
        comparisons: dict[str, Callable[[object, object], bool]] = {
            '==': operator.eq,
            '!=': operator.ne,
            '<': cast(Callable[[object, object], bool], operator.lt),
            '<=': cast(Callable[[object, object], bool], operator.le),
            '>': cast(Callable[[object, object], bool], operator.gt),
            '>=': cast(Callable[[object, object], bool], operator.ge),
        }
        if node.value in comparisons:
            return comparisons[node.value](left, right)
    if node.kind == 'call' and node.operands[0].kind == 'name':
        name = node.operands[0].value
        signature = SIGNATURES[name]
        arguments = {
            a.name or signature[i][0]: child(a.expression) for i, a in enumerate(node.arguments)
        }
        return convert(name, tuple(arguments[name] for name, _ in signature))
    raise ValueError('constant expressions cannot read fields, inputs or non-normal results')
