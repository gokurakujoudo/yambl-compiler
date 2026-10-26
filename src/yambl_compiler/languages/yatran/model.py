"""Represent execution records and reversible business data images."""

from dataclasses import dataclass

from ..shared.model import Condition, Value
from ..yaplan.model import Plan


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
