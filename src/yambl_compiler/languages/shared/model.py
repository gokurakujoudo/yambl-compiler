"""Shared literal values and coordinates for data languages."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Symbol:
    """Retain a model name or a stored result state.

    :param name: Qualified identifier, never an expression.
    """

    name: str


@dataclass(frozen=True)
class ObjectValue:
    """Preserve typed mapping keys without silently overwriting duplicates.

    :param items: Ordered literal key/value pairs.
    """

    items: tuple[tuple[Value, Value], ...]


type Value = str | int | bool | Decimal | Symbol | tuple[Value, ...] | ObjectValue


@dataclass(frozen=True)
class Condition:
    """Select one coordinate or a half-open interval.

    :param name: ID, dimension or declared temporal parameter.
    :param value: Equality value, or inclusive interval start.
    :param end: Exclusive interval end; None for equality.
    """

    name: str
    value: Value
    end: Value | None = None
