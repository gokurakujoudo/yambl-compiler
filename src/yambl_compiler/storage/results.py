"""Typed read outcomes without implicit fallback or propagation operators."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Literal


class Status(StrEnum):
    """Language-defined failure states and the payload-carrying PARTIAL state."""

    NOT_DEFINED = 'NOT_DEFINED'
    EMPTY = 'EMPTY'
    NO_ENTRY = 'NO_ENTRY'
    NO_DATA = 'NO_DATA'
    ENTITY_NOT_FOUND = 'ENTITY_NOT_FOUND'
    ERROR = 'ERROR'
    NOT_APPLICABLE = 'NOT_APPLICABLE'
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class Success[T]:
    """Return a normal value with provenance.

    :param value: Detached payload or explicit session-bound entity handle.
    :param origin: Storage, default, configuration-group or computation provenance.
    :param groups: Matching highest-priority configuration group IDs, when applicable.
    """

    value: T
    origin: Literal['storage', 'model_default', "config_group", "computation"] = 'storage'
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalculationIssue:
    """Locate an unusable contribution without losing duplicate business rows.

    :param status: Original non-normal result.
    :param field: Logical field identity.
    :param entity: Actual entity type, when available.
    :param id: Encoded instance ID, when available.
    :param coordinates: Date and dimension values in canonical order.
    :param contribution: Request and nested collection row positions.
    :param message: Additional diagnostic context.
    """

    status: Status
    field: str = ""
    entity: str = ""
    id: str = ""
    coordinates: tuple[tuple[str, str], ...] = ()
    contribution: tuple[int, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class NonNormalResult:
    """Return an explicit non-normal outcome.

    :param status: Language result constant.
    :param message: Optional diagnostic, without SQL payloads.
    :param issues: Located calculation failures, if any.
    """

    status: Status
    message: str = ''
    issues: tuple[CalculationIssue, ...] = ()

    def __post_init__(self) -> None:
        """Reject a payload-free PARTIAL result.

        :returns: None.
        :raises ValueError: If PARTIAL is used without a payload.
        """
        if self.status == Status.PARTIAL:
            raise ValueError("PARTIAL requires a value and issue details; use Partial")


@dataclass(frozen=True)
class Partial[T]:
    """Carry usable computed data together with omitted contribution details.

    :param value: Immutable usable payload.
    :param issues: Nonempty omission details; ERROR is not best-effort recoverable.
    :param status: Class constant; always PARTIAL and not a constructor parameter.
    """

    value: T
    issues: tuple[CalculationIssue, ...]
    status: ClassVar[Status] = Status.PARTIAL

    def __post_init__(self) -> None:
        """Require actual non-error omissions for a partial result.

        :returns: None.
        :raises ValueError: On missing details or an invalid omission status.
        """
        if not self.issues or any(i.status in (Status.ERROR, Status.PARTIAL) for i in self.issues):
            raise ValueError("PARTIAL requires non-error omission details")


type ReadResult[T] = Success[T] | Partial[T] | NonNormalResult


@dataclass(frozen=True)
class EntityRef[T]:
    """Hold an entity ID without fetching the target.

    :param target: Qualified declared entity type.
    :param id: Target ID value.
    """

    target: str
    id: str | int
