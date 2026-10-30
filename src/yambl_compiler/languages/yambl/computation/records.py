"""Immutable calculation syntax, independent of parsers and storage backends."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Record:
    """Mark supported portable records for explicit contract serialization."""


@dataclass(frozen=True)
class Value(Record):
    """Preserve a scalar or structured value without JSON numeric rounding.

    :param kind: Value category.
    :param text: Normalized scalar value or reference ID.
    :param target: Qualified type or dictionary identity.
    :param selections: Canonical selected flags codes.
    :param mask: Decimal flags bit mask, empty for other values.
    :param fields: Named members of a structured compile-time value.
    """

    kind: Literal[
        'string',
        'integer',
        'decimal',
        'bool',
        'reference',
        'type',
        'dictionary',
        'code',
        'flags',
        'float',
        'date',
        'datetime',
        'snowflake_id',
        'structured',
    ]
    text: str
    target: str
    selections: tuple[str, ...] = ()
    mask: str = ''
    fields: tuple['ValueField', ...] = ()


@dataclass(frozen=True)
class ValueField(Record):
    """Preserve a structured value field and its typed member.

    :param name: Declared field name.
    :param value: Checked field value.
    """

    name: str
    value: Value


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
    :param annotation: Contextual literal type or structured-member receiver type.
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
        "sequence",
        "object",
    ]
    value: str
    operands: tuple[Expression, ...]
    arguments: tuple[Argument, ...]
    cases: tuple[Case, ...]
    span: Span
    annotation: TypeHint | None = None


@dataclass(frozen=True)
class HintParameter(Record):
    """Preserve a primitive type parameter.

    :param name: Parameter name, empty for positional syntax.
    :param value: Literal spelling, including quotes for string parameters.
    """

    name: str
    value: str


@dataclass(frozen=True)
class HintAccess(Record):
    """Preserve a type or descriptor member selection.

    :param operator: Dot or descriptor selection.
    :param name: Selected member.
    """

    operator: Literal['.', '::']
    name: str


@dataclass(frozen=True)
class TypeHint(Record):
    """Carry a local type annotation independently of the parser.

    :param name: Primitive, declared name or quoted external dictionary name.
    :param arguments: Generic arguments.
    :param parameters: Primitive parameters in source order.
    :param accesses: Ordered descriptor selections.
    :param span: Original type location.
    :param target: Resolved declaration identity for elaborated literals.
    :param operations: Flags operations in source order.
    :param binding: Local generic binding name, when present.
    :param finite_candidates: Compile-time expressions inside ``one_of``.
    :param finite_values: Normalized finite members of the checked annotation.
    """

    name: str
    arguments: tuple[TypeHint, ...]
    parameters: tuple[HintParameter, ...]
    accesses: tuple[HintAccess, ...]
    span: Span
    target: str = ''
    operations: tuple[HintOperation, ...] = ()
    binding: str = ''
    finite_candidates: tuple[Expression, ...] = ()
    finite_values: tuple[Value, ...] = ()


@dataclass(frozen=True)
class HintOperation(Record):
    """Describe a flags operation inside a type annotation.

    :param operator: Union or removal.
    :param operand: Right-hand operand.
    """

    operator: Literal['+', '-']
    operand: TypeHint


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
    :param annotation: Optional explicit local type.
    """

    name: str
    expression: Expression
    span: Span
    annotation: TypeHint | None = None


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
