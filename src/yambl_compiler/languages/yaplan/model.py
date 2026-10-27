"""Represent grouped model-bound data plans."""

from dataclasses import dataclass, field
from typing import Literal

from ..shared.model import Condition, Value


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
    :param expect: Optional required match cardinality before mutation.
    """

    kind: Literal['insert', 'update', 'delete', 'replace']
    target: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Value, ...], ...]
    scope: tuple[Condition, ...] = ()
    owned_data: bool = False
    line: int = field(default=1, compare=False)
    expect: Literal['one', 'any'] | None = None


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
