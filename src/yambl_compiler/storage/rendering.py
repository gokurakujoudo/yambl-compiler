"""Render SQLite DDL and reference documentation from the same tables."""

import json
import re
from dataclasses import asdict

from yambl_compiler.storage.layout import Layout, Table, quote, sql_literal, type_label


def table_sql(table: Table) -> str:
    """Render one table and its indexes.

    :param table: Physical table definition.
    :returns: Executable SQLite DDL.
    """
    clauses = [
        f'{quote(c.name)} {c.sql_type}'
        + ('' if c.nullable else ' NOT NULL')
        + (f' DEFAULT {c.default}' if c.default else '')
        for c in table.columns
    ]
    clauses.append('PRIMARY KEY (' + ', '.join(quote(k) for k in table.primary) + ')')
    clauses.extend('CHECK (' + check + ')' for check in table.checks)
    clauses.extend(table.foreign_keys)
    text = f'CREATE TABLE {quote(table.name)} (\n    ' + ',\n    '.join(clauses) + '\n);\n'
    for i, columns in enumerate(table.indexes):
        text += f'CREATE INDEX {quote(table.name + "__index_" + str(i))} ON {quote(table.name)} ({", ".join(quote(c) for c in columns)});\n'
    return text


def ddl(layout: Layout) -> str:
    """Render the complete schema and identity metadata.

    :param layout: Validated physical model.
    :returns: SQL suitable for initializing a new database.
    """
    sql = 'PRAGMA foreign_keys = ON;\n\n' + '\n'.join(table_sql(t) for t in layout.tables)
    for key, value in (('layout_revision', '1'), ('model_digest', layout.model.model_digest)):
        sql += f'INSERT INTO yambl_metadata (key, value) VALUES ({sql_literal(key)}, {sql_literal(value)});\n'
    return sql


def markdown_cell(text: str) -> str:
    """Escape text inside a Markdown table cell.

    :param text: Untrusted model description.
    :returns: One safe cell, preserving visible text.
    """
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('|', '&#124;')
        .replace('`', '&#96;')
        .replace('\n', '<br>')
        .replace('\r', '')
    )


def reference(layout: Layout) -> str:
    """Document every physical object with its actual DDL.

    :param layout: Shared storage model.
    :returns: Standalone Markdown reference.
    """
    lines = [
        '# SQLite schema reference',
        '',
        f'Model SHA-256: `{layout.model.model_digest}`.',
        f'IR revision: {layout.model.format_revision}. Storage layout revision: 1.',
        '',
        'Each field state is VALUE, NO_ENTRY, EMPTY or NOT_APPLICABLE. A missing row is NO_ENTRY. '
        'EMPTY has a NULL payload. An existing VALUE collection header with no members is an empty collection.',
        '',
        'Snapshot dates match exactly. SCD periods use [valid_from, valid_to); both boundaries are required, '
        'between 1900-01-01 and 2999-12-31. The reader reports overlapping periods as ERROR.',
        '',
        'Entity references are logical links. Their target existence is checked by explicit resolution, '
        'not by a target foreign key. Collection members have a physical foreign key to their header. '
        'Enable PRAGMA foreign_keys on every writing connection.',
        '',
        '## Tables',
        '',
    ]
    lines.extend(f'- [{t.name}](#table-{i})' for i, t in enumerate(layout.tables))
    for i, table in enumerate(layout.tables):
        lines.extend(
            [
                '',
                f'<a id="table-{i}"></a>',
                '',
                f'## {table.name}',
                '',
                table.purpose,
                '',
                '| Column | SQLite type | Nullable | SQL default | Meaning / YAMBL type |',
                '| --- | --- | --- | --- | --- |',
            ]
        )
        for col in table.columns:
            cells = [
                col.name,
                col.sql_type,
                'yes' if col.nullable else 'no',
                col.default or 'none',
                col.meaning,
            ]
            lines.append('| ' + ' | '.join(markdown_cell(c) for c in cells) + ' |')
        lines.extend(
            [
                '',
                'Primary key / uniqueness: ' + ', '.join(table.primary) + '.',
                '',
                'Foreign keys: ' + ('; '.join(table.foreign_keys) or 'none') + '.',
                '',
                'Indexes: '
                + ('; '.join(', '.join(x) for x in table.indexes) or 'primary key only')
                + '.',
                '',
            ]
        )
        for identity in table.fields:
            stored = layout.fields[identity]
            lines.append(
                f'Logical field: `{identity}`; category: `{stored.field.category}`; type: {markdown_cell(type_label(stored.field.type))}.'
            )
            for d in layout.model.definitions:
                for f in d.fields:
                    if f.identity == identity:
                        default = f.default.text if f.default else 'none'
                        lines.append(
                            f'- Implementation `{f.implementation}`; model default: {markdown_cell(default)}. '
                            'Model defaults are applied by the reader only for NO_ENTRY; they are not SQL defaults.'
                        )
            lines.append('')
        sql = table_sql(table).rstrip()
        fence = '`' * max(3, max((len(m) for m in re.findall(r'`+', sql)), default=0) + 1)
        lines.extend([fence + 'sql', sql, fence])
    return '\n'.join(lines) + '\n'


def manifest(layout: Layout) -> str:
    """Serialize physical mappings for external writers.

    :param layout: Shared storage model.
    :returns: Deterministic readable JSON manifest.
    """
    data = {
        'layout_revision': 1,
        'model_digest': layout.model.model_digest,
        'roots': layout.roots,
        'tables': [asdict(t) for t in layout.tables],
        'fields': {k: asdict(v) for k, v in layout.fields.items()},
    }
    return json.dumps(data, ensure_ascii=False, indent=4) + '\n'
