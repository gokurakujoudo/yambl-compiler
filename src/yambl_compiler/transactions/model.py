"""Keep transaction syntax independent of compiler and backend dependencies."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


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


@dataclass(frozen=True)
class Operation:
    """Describe one homogeneous batch or field scope.

    :param kind: Requested mutation.
    :param target: Qualified entity or entity field.
    :param columns: Data columns, in row order.
    :param rows: Literal data records.
    :param scope: Field selection conditions.
    :param owned_data: Whether entity deletion includes owned records.
    :param line: One-based source line of the operation.
    """

    kind: Literal['insert', 'update', 'delete', 'replace']
    target: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Value, ...], ...]
    scope: tuple[Condition, ...] = ()
    owned_data: bool = False
    line: int = field(default=1, compare=False)


@dataclass(frozen=True)
class Group:
    """Name independently selectable rollback work.

    :param name: Unique group identifier.
    :param operations: Source-ordered operations.
    """

    name: str
    operations: tuple[Operation, ...]


@dataclass(frozen=True)
class Plan:
    """Bind a grouped data plan to one model revision.

    :param name: Human-readable plan identifier.
    :param model: Model semantic SHA-256.
    :param groups: Named source-ordered groups.
    :param version: Document format revision.
    """

    name: str
    model: str
    groups: tuple[Group, ...]
    version: int = 1


@dataclass(frozen=True)
class Image:
    """Record a typed business data image, including an empty result.

    :param target: Qualified entity or field whose rows are recorded.
    :param columns: Business column names.
    :param rows: Located business data; an empty tuple records absence.
    """

    target: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Value, ...], ...]


@dataclass(frozen=True)
class Change:
    """Record one independently comparable rollback unit.

    :param target: Entity or logical field affected by this unit.
    :param scope: Actual resolved coordinates, including the owner ID.
    :param before: Located images before the operation.
    :param after: Images actually committed by the operation.
    """

    target: str
    scope: tuple[Condition, ...]
    before: tuple[Image, ...]
    after: tuple[Image, ...]


@dataclass(frozen=True)
class TransactionGroup:
    """Retain actual changes and skipped conflicts for one plan group.

    :param name: Original plan group name.
    :param changes: Independently reversible changes in execution order.
    :param warnings: Conflict messages without complete business payloads.
    """

    name: str
    changes: tuple[Change, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Transaction:
    """Keep a portable, model-bound execution record.

    :param id: Transaction UUID.
    :param time: UTC execution timestamp in ISO format, ending in Z.
    :param plan: Original normalized plan.
    :param groups: Executed groups and their actual results.
    :param source: Source transaction UUID for rollback, empty for apply.
    :param digest: SHA-256 of canonical semantic contents.
    :param version: Transaction format revision.
    """

    id: str
    time: str
    plan: Plan
    groups: tuple[TransactionGroup, ...]
    source: str = ''
    digest: str = ''
    version: int = 1
