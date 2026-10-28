"""Render SQLite DDL and reference documentation from the same tables."""

import json
import re
from dataclasses import asdict

from yambl_compiler.storage.layout import (
    LAYOUT_REVISION,
    Layout,
    Table,
    quote,
    sql_literal,
    table_name,
    type_label,
)


def table_sql(table: Table) -> str:
    """Render one table and its indexes.

    :param table: Physical table definition.
    :returns: Executable SQLite DDL.
    """
    clauses = [
        f"{quote(c.name)} {c.sql_type}"
        + ("" if c.nullable else " NOT NULL")
        + (f" DEFAULT {c.default}" if c.default else "")
        for c in table.columns
    ]
    clauses.append("PRIMARY KEY (" + ", ".join(quote(k) for k in table.primary) + ")")
    clauses.extend("CHECK (" + check + ")" for check in table.checks)
    clauses.extend(table.foreign_keys)
    text = f"CREATE TABLE {quote(table.name)} (\n    " + ",\n    ".join(clauses) + "\n);\n"
    for i, columns in enumerate(table.indexes):
        text += f'CREATE INDEX {quote(table.name + "__index_" + str(i))} ON {quote(table.name)} ({", ".join(quote(c) for c in columns)});\n'
    return text


def ddl(layout: Layout) -> str:
    """Render the complete schema and identity metadata.

    :param layout: Validated physical model.
    :returns: SQL suitable for initializing a new database.
    """
    sql = "PRAGMA foreign_keys = ON;\n\n" + "\n".join(table_sql(t) for t in layout.tables)
    for table in layout.tables:
        for row in table.rows:
            sql += f'INSERT INTO {quote(table.name)} VALUES ({", ".join(sql_literal(v) for v in row)});\n'
    for key, value in (
        ("layout_revision", str(LAYOUT_REVISION)),
        ("model_digest", layout.model.model_digest),
    ):
        sql += f"INSERT INTO tb_yambl_metadata (key, value) VALUES ({sql_literal(key)}, {sql_literal(value)});\n"
    return sql


def markdown_cell(text: str) -> str:
    """Escape text inside a Markdown table cell.

    :param text: Untrusted model description.
    :returns: One safe cell, preserving visible text.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("`", "&#96;")
        .replace("\n", "<br>")
        .replace("\r", "")
    )


def reference(layout: Layout) -> str:
    """Document every physical object with its actual DDL.

    :param layout: Shared storage model.
    :returns: Standalone Markdown reference.
    """
    lines = [
        "# SQLite schema reference",
        "",
        f"Model SHA-256: `{layout.model.model_digest}`.",
        f"IR revision: {layout.model.format_revision}. Storage layout revision: {LAYOUT_REVISION}.",
        "",
        "Each field state is VALUE, NO_ENTRY, EMPTY or NOT_APPLICABLE. A missing row is NO_ENTRY. "
        "EMPTY has a NULL payload. An existing VALUE collection header with no members is an empty collection.",
        "",
        "Snapshot dates match exactly. SCD periods use [valid_from, valid_to); both boundaries are required, "
        "between 1900-01-01 and 2999-12-31. The reader reports overlapping periods as ERROR.",
        "",
        "Entity references are logical links. Their target existence is checked by explicit resolution, "
        "not by a target foreign key. Collection members have a physical foreign key to their header. "
        "Enable PRAGMA foreign_keys on every writing connection.",
        "",
        "Dimensionless static fields without a storage group share the root identity table. Their collection states also live "
        "there; members use a separate field table. Other collection headers use the _status suffix.",
        "",
        "Sparse and dense groups use the root table name plus the category suffix. Dense groups have "
        "one payload and state column per field. Sparse groups use (id, field) keys, one state column "
        "and typed string/bool/int/decimal/date payload columns as needed. VALUE uses exactly one lane; "
        "non-normal states use NULL in every lane. Integer and decimal lanes use exact TEXT. "
        "References use the target ID lane. These groups do not change field identity or reading semantics.",
        "",
        "Configuration uses one long table per root entity, keyed by (id, field), with state and typed "
        "string/bool/int/decimal/date payload columns as needed. It shares the sparse layout rules: "
        "one corresponding lane for VALUE scalars and NULL in all lanes for non-normal states. "
        "Collection headers use NULL scalar lanes and separate member tables. Reader defaults are unchanged.",
        "",
        "Built-in tables store Decodes, Hierarchy and ConfigGroup. Declared dictionaries, including "
        "generated None/All flags entries, are inserted from the embedded IR. Built-in member tables "
        "have foreign keys to their owner. These tables have no generated read methods or selection engine.",
        "",
        "ConfigGroup mapping values are JSON objects with type (a resolved IR Type object) and value "
        "(the JSON payload). SQL checks the envelope; field membership, type compatibility and configuration "
        "execution are not implemented for these storage-only mappings.",
        "",
        "## Tables",
        "",
    ]
    lines.extend(f"- [{t.name}](#table-{i})" for i, t in enumerate(layout.tables))
    for i, table in enumerate(layout.tables):
        lines.extend(
            [
                "",
                f'<a id="table-{i}"></a>',
                "",
                f"## {table.name}",
                "",
                table.purpose,
                "",
                "| Column | SQLite type | Nullable | SQL default | Meaning / YAMBL type |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for col in table.columns:
            cells = [
                col.name,
                col.sql_type,
                "yes" if col.nullable else "no",
                col.default or "none",
                col.meaning,
            ]
            lines.append("| " + " | ".join(markdown_cell(c) for c in cells) + " |")
        lines.extend(
            [
                "",
                "Primary key / uniqueness: " + ", ".join(table.primary) + ".",
                "",
                "Foreign keys: " + ("; ".join(table.foreign_keys) or "none") + ".",
                "",
                "Indexes: "
                + ("; ".join(", ".join(x) for x in table.indexes) or "primary key only")
                + ".",
                "",
            ]
        )
        for identity in table.fields:
            stored = layout.fields[identity]
            lines.append(
                f"Logical field: `{identity}`; category: `{stored.field.category}`; type: {markdown_cell(type_label(stored.field.type))}."
            )
            for d in layout.model.definitions:
                for f in d.fields:
                    if f.identity == identity:
                        default = f.default.text if f.default else "none"
                        lines.append(
                            f"- Implementation `{f.implementation}`; model default: {markdown_cell(default)}. "
                            "Model defaults are applied by the reader only for NO_ENTRY; they are not SQL defaults."
                        )
            lines.append("")
        if table.rows:
            lines.extend(
                [
                    f"Initial rows from embedded dictionary declarations: {len(table.rows)}.",
                    "",
                ]
            )
        sql = table_sql(table).rstrip()
        fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", sql)), default=0) + 1)
        lines.extend([fence + "sql", sql, fence])
    return "\n".join(lines) + "\n"


def manifest(layout: Layout) -> str:
    """Serialize physical mappings for external writers.

    :param layout: Shared storage model.
    :returns: Deterministic readable JSON manifest.
    """
    roots = {**layout.roots, **{v.root: v.root for v in layout.fields.values()}}
    data = {
        "layout_revision": LAYOUT_REVISION,
        "model_digest": layout.model.model_digest,
        "roots": roots,
        "identity_tables": {root: table_name(root) for root in sorted(set(roots.values()))},
        "tables": [asdict(t) for t in layout.tables],
        "fields": {
            k: {
                **asdict(v),
                "readable": v.root in layout.entities
                and layout.entities[v.root].source.origin != "builtin",
            }
            for k, v in layout.fields.items()
        },
    }
    return json.dumps(data, ensure_ascii=False, indent=4) + "\n"
