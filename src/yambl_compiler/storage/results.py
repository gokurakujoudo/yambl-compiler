"""Typed read outcomes without implicit fallback or propagation operators."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Status(StrEnum):
    """The seven language-defined non-normal results."""

    NOT_DEFINED = 'NOT_DEFINED'
    EMPTY = 'EMPTY'
    NO_ENTRY = 'NO_ENTRY'
    NO_DATA = 'NO_DATA'
    ENTITY_NOT_FOUND = 'ENTITY_NOT_FOUND'
    ERROR = 'ERROR'
    NOT_APPLICABLE = 'NOT_APPLICABLE'


@dataclass(frozen=True)
class Success[T]:
    """Return a normal value with provenance.

    :param value: Detached payload or explicit session-bound entity handle.
    :param origin: Storage or model-default provenance.
    """

    value: T
    origin: Literal['storage', 'model_default'] = 'storage'


@dataclass(frozen=True)
class NonNormalResult:
    """Return an explicit non-normal outcome.

    :param status: Language result constant.
    :param message: Optional diagnostic, without SQL payloads.
    """

    status: Status
    message: str = ''


type ReadResult[T] = Success[T] | NonNormalResult


@dataclass(frozen=True)
class EntityRef[T]:
    """Hold an entity ID without fetching the target.

    :param target: Qualified declared entity type.
    :param id: Target ID value.
    """

    target: str
    id: str | int
