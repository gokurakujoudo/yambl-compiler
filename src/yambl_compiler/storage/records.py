"""Portable physical records shared by generation and read backends."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from yambl_compiler.ir.model import Bound, Definition, Field, Model

LAYOUT_REVISION = 9
FILE_REVISION = 3
STATE_CODES = {'VALUE': 0, 'NO_ENTRY': 1, 'EMPTY': 2, 'NOT_APPLICABLE': 3}
STATE_NAMES = {value: name for name, value in STATE_CODES.items()}


def quote(name: str) -> str:
    """Quote a SQLite identifier.

    :param name: Identifier, never SQL source.
    :returns: Double-quoted identifier.
    """
    return '"' + name.replace('"', '""') + '"'


def sql_literal(text: str) -> str:
    """Quote a SQL text constant.

    :param text: Literal payload.
    :returns: Escaped text literal.
    """
    return "'" + text.replace("'", "''") + "'"


def snake(name: str) -> str:
    """Name a generated Python method or SQL table.

    :param name: Qualified or PascalCase identifier.
    :returns: Lowercase underscore spelling.
    """
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name.replace(".", "__")).lower()


def table_name(identity: str) -> str:
    """Name an entity table without leaking compiler identity delimiters.

    :param identity: Qualified entity identity.
    :returns: Namespace and entity segments joined after ``tb_``.
    """
    return "tb_" + "_".join(snake(part) for part in identity.removeprefix("builtins.").split("."))


@dataclass(frozen=True)
class Column:
    """Describe a physical column.

    :param name: SQL name.
    :param sql_type: SQLite storage type.
    :param nullable: Whether NULL is allowed.
    :param default: SQL default expression, empty when absent.
    :param meaning: User-facing description and logical type.
    """

    name: str
    sql_type: str = "TEXT"
    nullable: bool = False
    default: str = ""
    meaning: str = ""


@dataclass
class Table:
    """Describe a generated table without parsing DDL.

    :param name: Physical table name.
    :param purpose: Table role and logical owner.
    :param columns: Ordered columns.
    :param primary: Ordered primary key columns.
    :param checks: CHECK predicate SQL expressions.
    :param foreign_keys: Explicit foreign key clauses.
    :param indexes: Ordered column lists for additional indexes.
    :param fields: Logical field identities represented here.
    :param rows: Initial rows in column order, sourced from embedded declarations.
    :param parents: Physical foreign-key target tables, retained for diagrams.
    """

    name: str
    purpose: str
    columns: list[Column]
    primary: tuple[str, ...]
    checks: list[str] = field(default_factory=list[str])
    foreign_keys: list[str] = field(default_factory=list[str])
    indexes: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    fields: list[str] = field(default_factory=list[str])
    rows: list[tuple[str | int, ...]] = field(default_factory=list[tuple[str | int, ...]])
    parents: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class StoredField:
    """Map one logical field to its physical record.

    :param field: Representative declaration.
    :param root: Root entity identity, including storage-only built-ins.
    :param table: Header or scalar table, empty for a derived reverse relationship.
    :param state: Per-field state column.
    :param value: Scalar payload column, empty for collections.
    :param members: Collection member table, empty for scalars.
    :param dimensions: Required dimension bindings, sorted by key.
    :param key_values: Fixed discriminators needed to select this logical field.
    """

    field: Field
    root: str
    table: str
    state: str
    value: str
    members: str
    dimensions: tuple[Bound, ...]
    key_values: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Layout:
    """Keep validated semantic input alongside its physical layout.

    :param model: Complete IR.
    :param entities: Supported business entities by identity.
    :param roots: Entity to business root mapping.
    :param discriminators: Whether each root identity table stores actual types.
    :param ids: Root to unique ID declaration mapping.
    :param fields: Logical fields to storage records.
    :param tables: Complete physical schema.
    """

    model: Model
    entities: dict[str, Definition]
    roots: dict[str, str]
    discriminators: dict[str, bool]
    ids: dict[str, Field]
    fields: dict[str, StoredField]
    tables: tuple[Table, ...]


def id_column(layout: Layout, root: str) -> str:
    """Return the physical identity column for an inheritance root.

    :param layout: Validated storage layout.
    :param root: Entity root identity.
    :returns: Built-in ID field name or the business table's shared ID column.
    """
    return layout.ids[root].name if layout.entities[root].source.origin == 'builtin' else 'id'


def actual_type(layout: Layout, root: str, row: Mapping[str, object]) -> str:
    """Resolve the actual entity type from its identity table.

    :param layout: Physical layout with root discriminator information.
    :param root: Inheritance root identity.
    :param row: Identity row or projection.
    :returns: Qualified actual entity type.
    """
    return str(row['entity_type']) if layout.discriminators[root] else root


def mapped_field(layout: Layout, identity: str) -> tuple[Field, str] | None:
    """Resolve a stored or derived field and its inheritance root.

    :param layout: Generated model and storage layout.
    :param identity: Logical field identity.
    :returns: Representative declaration and root, or None if unavailable.
    """
    stored = layout.fields.get(identity)
    if stored is not None:
        return stored.field, stored.root
    for definition in layout.model.definitions:
        if definition.identity != identity.rpartition('.')[0]:
            continue
        for declaration in definition.fields:
            if declaration.builtin and declaration.identity == identity:
                return declaration, layout.roots.get(definition.identity, definition.identity)
    return None
