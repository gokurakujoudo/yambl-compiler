"""Immutable calculation syntax, independent of parsers and storage backends."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Record:
    """Mark supported portable records for explicit contract serialization."""


@dataclass(frozen=True)
class CalculationSpan(Record):
    """Locate an expression within its declaring source.

    :param start: Zero-based character offset.
    :param end: Exclusive character offset.
    :param line: One-based starting line.
    :param column: One-based starting column.
    :param end_line: One-based ending line.
    :param end_column: Exclusive ending column.
    """

    start: int
    end: int
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class CalculationExpression(Record):
    """Represent a calculation node without retaining a parser tree.

    :param kind: Node discriminator.
    :param value: Identifier, decoded literal, operator or selected member.
    :param operands: Ordered children; calls start with their receiver.
    :param arguments: Call arguments in source order.
    :param cases: Recovery branches in source order.
    :param span: Original expression location.
    """

    kind: Literal[
        "name",
        "string",
        "integer",
        "decimal",
        "bool",
        "member",
        "descriptor",
        "call",
        "unary",
        "binary",
        "conditional",
        "lambda",
        "recover",
    ]
    value: str
    operands: tuple[Expression, ...]
    arguments: tuple[Argument, ...]
    cases: tuple[Case, ...]
    span: Span


@dataclass(frozen=True)
class CalculationArgument(Record):
    """Preserve explicit named and positional calculation arguments.

    :param name: Parameter name, empty for positional arguments.
    :param expression: Argument value.
    :param span: Invocation argument location.
    """

    name: str
    expression: Expression
    span: Span


@dataclass(frozen=True)
class RecoveryCase(Record):
    """Describe a lazy recovery branch.

    :param statuses: Matched result names in source order.
    :param binding: Optional local name for a PARTIAL payload.
    :param expression: Replacement result expression.
    :param span: Branch location.
    """

    statuses: tuple[str, ...]
    binding: str
    expression: Expression
    span: Span


@dataclass(frozen=True)
class CalculationBinding(Record):
    """Introduce one immutable local calculation value.

    :param name: Local identifier.
    :param expression: Bound expression.
    :param span: Declaration location.
    """

    name: str
    expression: Expression
    span: Span


@dataclass(frozen=True)
class CalculationBody(Record):
    """Retain immutable locals and the single final return expression.

    :param bindings: Ordered local definitions.
    :param result: Final return expression.
    :param span: Full body location.
    """

    bindings: tuple[Binding, ...]
    result: Expression
    span: Span


Span = CalculationSpan
Expression = CalculationExpression
Argument = CalculationArgument
Case = RecoveryCase
Binding = CalculationBinding
Body = CalculationBody
