"""Typed internal services shared by batch execution and the public reader."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from yambl_compiler.ir.model import Field, Type
from yambl_compiler.storage.backends import Backend, Row
from yambl_compiler.storage.records import Layout, StoredField
from yambl_compiler.storage.results import ReadResult
from yambl_compiler.storage.scalars import ScalarRule


@dataclass(frozen=True)
class Session:
    """Expose the internal read services needed by computation stages.

    :param layout: Validated model and physical layout.
    :param backend: Pinned storage backend.
    :param implementations: Actual field declarations.
    :param code_types: Generated dictionary code classes.
    :param ready: Check that new backend reads remain allowed.
    :param encode: Validate and encode a typed input.
    :param decode: Decode one stored scalar payload.
    :param point: Interpret point records with the common result rules.
    :param value: Interpret one stored field row.
    :param scalar_rule: Freeze a scalar's input validation without retaining the session.
    """

    layout: Layout
    backend: Backend
    implementations: dict[str, Field]
    code_types: dict[str, type[str]]
    ready: Callable[[], None]
    encode: Callable[[Type, object], str | int]
    decode: Callable[[Type, object], object]
    point: Callable[[StoredField, Field, list[Row], date | None], ReadResult[object]]
    value: Callable[[StoredField, Field, Row], ReadResult[object]]
    scalar_rule: Callable[[Type], ScalarRule]
