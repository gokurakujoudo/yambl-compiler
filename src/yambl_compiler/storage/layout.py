"""Build one physical model shared by SQL, documentation and readers."""

import json
import keyword
import re
from dataclasses import dataclass, field

from yambl_compiler.ir.codec import encode
from yambl_compiler.ir.model import Bound, Definition, Field, Model, Type

LAYOUT_REVISION = 2


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
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name.replace('.', '__')).lower()


def table_name(identity: str) -> str:
    """Name an entity table without leaking compiler identity delimiters.

    :param identity: Qualified entity identity.
    :returns: Namespace and entity segments joined after ``tb_``.
    """
    return 'tb_' + '_'.join(snake(part) for part in identity.removeprefix('builtins.').split('.'))


def type_label(type_: Type) -> str:
    """Describe a supported resolved type for the schema reference.

    :param type_: Resolved field or dimension type.
    :returns: Qualified type with generic arguments, parameters and exact constraint.
    """
    text = type_.target or type_.name
    if type_.arguments:
        text += '<' + ', '.join(type_label(arg) for arg in type_.arguments) + '>'
    if type_.parameters:
        text += '[' + ', '.join(p.name + ': ' + p.value for p in type_.parameters) + ']'
    if type_.exact is not None:
        exact = type_.exact
        text += ' = ' + (
            f'{exact.target}[{" + ".join(exact.selections) or "None"}; mask={exact.mask}]'
            if exact.kind == 'flags'
            else exact.text
        )
    return text


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
    sql_type: str = 'TEXT'
    nullable: bool = False
    default: str = ''
    meaning: str = ''


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
    """

    name: str
    purpose: str
    columns: list[Column]
    primary: tuple[str, ...]
    checks: list[str] = field(default_factory=list[str])
    foreign_keys: list[str] = field(default_factory=list[str])
    indexes: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    fields: list[str] = field(default_factory=list[str])
    rows: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])


@dataclass(frozen=True)
class StoredField:
    """Map one logical field to its physical record.

    :param field: Representative declaration.
    :param root: Root entity identity, including storage-only built-ins.
    :param table: Header or scalar table.
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
    :param ids: Root to unique ID declaration mapping.
    :param fields: Logical fields to storage records.
    :param tables: Complete physical schema.
    """

    model: Model
    entities: dict[str, Definition]
    roots: dict[str, str]
    ids: dict[str, Field]
    fields: dict[str, StoredField]
    tables: tuple[Table, ...]


def supported(type_: Type, entities: dict[str, Definition], model: Model) -> bool:
    """Check the first backend's supported storage types.

    :param type_: Resolved type.
    :param entities: Business entity definitions.
    :param model: Embedded dictionary definitions.
    :returns: Whether the type has implemented storage and read semantics.
    """
    if type_.dependent:
        return False
    if type_.kind == 'reference':
        return type_.target in entities
    if type_.name == 'collection':
        return len(type_.arguments) == 1 and type_.arguments[0].target in entities
    if type_.name == 'code':
        target = type_.arguments[0].target
        return not any(
            d.identity == target and any(c.target == 'builtins.flags' for c in d.decorators)
            for d in model.definitions
        )
    return not type_.target and type_.name in (
        'string',
        'bool',
        'integer',
        'snowflake_id',
        'decimal',
        'date',
    )


def build_layout(model: Model) -> Layout:
    """Preflight supported business fields and construct deterministic tables.

    :param model: Semantically validated IR.
    :returns: Shared physical model.
    :raises ValueError: On unsupported types, IDs or generated-name collisions.
    """
    entities = {
        d.identity: d
        for d in model.definitions
        if d.kind == 'entity' and d.source.origin == 'model'
    }
    declarations = {f.implementation: f for d in model.definitions for f in d.fields}
    roots: dict[str, str] = {}
    ids: dict[str, Field] = {}
    names: set[str] = set()
    for key, entity in entities.items():
        short = entity.name.rsplit('.', 1)[-1]
        method = snake(short)
        if short in names or method in names or keyword.iskeyword(method):
            raise ValueError(f'{key}: Python name collision')
        names.update((short, method))
        root_definition = entity
        while root_definition.parent in entities:
            root_definition = entities[root_definition.parent]
        roots[key] = root_definition.identity
        effective_ids = [
            declarations[e.implementation]
            for e in entity.effective
            if declarations[e.implementation].category == 'id'
        ]
        if len(effective_ids) != 1 or effective_ids[0].type.name not in (
            'string',
            'integer',
            'snowflake_id',
        ):
            raise ValueError(f'{key}: requires exactly one string or integer ID')
        id_ = effective_ids[0]
        if (
            root_definition.identity in ids
            and ids[root_definition.identity].implementation != id_.implementation
        ):
            raise ValueError(f'{key}: an inheritance tree must share its root ID')
        ids[root_definition.identity] = id_
    tables: dict[str, Table] = {}
    claims: dict[str, str] = {}

    def claim(name: str, owner: str) -> None:
        """Reject physical names shared by unrelated logical objects.

        :param name: Proposed physical name.
        :param owner: Logical table grouping identity.
        :returns: None when this name is available or already belongs to the group.
        :raises ValueError: On a conflicting table name.
        """
        if name in claims and claims[name] != owner:
            raise ValueError(f'{owner}: SQL table collision with {claims[name]}: {name}')
        claims[name] = owner

    metadata = Table(
        'tb_yambl_metadata', 'Model identity', [Column('key'), Column('value')], ('key',)
    )
    tables[metadata.name] = metadata
    claim(metadata.name, 'storage metadata')
    for root, id_ in sorted(ids.items()):
        name = table_name(root)
        claim(name, root)
        tables[name] = Table(
            name,
            f'Identity table for {root}',
            [
                Column('id', meaning=id_.implementation + ': ' + type_label(id_.type)),
                Column('entity_type', meaning='Actual qualified entity type'),
            ],
            ('id',),
            [
                f'{quote("entity_type")} IN ({", ".join(sql_literal(k) for k in roots if roots[k] == root)})'
            ],
        )
    stored: dict[str, StoredField] = {}
    groups: dict[tuple[str, str, str], str] = {}
    for logical in model.fields:
        impls = [declarations[i] for i in logical.implementations]
        f = impls[0]
        owner = f.implementation.rsplit('.', 1)[0]
        if owner not in entities or f.category == 'id':
            continue
        dimensions = tuple(
            sorted(
                (
                    b
                    for c in f.decorators
                    if c.target == 'builtins.dimension'
                    for b in c.bound
                    if b.name == 'variation'
                ),
                key=lambda b: b.key,
            )
        )
        for implementation in impls:
            if not supported(implementation.type, entities, model):
                s = implementation.source
                raise ValueError(
                    f'{s.path}:{s.line}:{s.column}: {implementation.implementation}: unsupported storage type {implementation.type.name}'
                )
        if any(not supported(b.type, entities, model) for b in dimensions):
            raise ValueError(f'{f.identity}: unsupported dimension type')
        if (
            keyword.iskeyword(f.name)
            or f.name in ('id', 'entity_type')
            or any(b.key == 'self' or keyword.iskeyword(b.key) for b in dimensions)
        ):
            raise ValueError(f'{f.identity}: generated Python parameter or field collision')
        root = roots[owner]
        signature = json.dumps(
            [{'key': b.key, 'type': encode(b.type)} for b in dimensions], sort_keys=True
        )
        collection = f.type.name == 'collection'
        config = f.category == 'config'
        base = table_name(root)
        member_name = base + '_' + f.name
        if config:
            name, owner_key = base + '_config', root + ':config'
        elif f.category == 'static' and not dimensions:
            name, owner_key = base, root
        elif f.category in ('snapshot', 'scd') or collection:
            name = member_name + ('_status' if collection else '')
            owner_key = f.identity
        else:
            group_key = (root, f.category, signature)
            if group_key not in groups:
                group_base = base + '_' + f.category + '_' + '_'.join(b.key for b in dimensions)
                name = group_base
                index = 2
                while name in groups.values():
                    name = group_base + '_' + str(index)
                    index += 1
                groups[group_key] = name
            name = groups[group_key]
            owner_key = repr(group_key)
        claim(name, owner_key)
        keys = ['id', *('dim_' + b.key for b in dimensions)]
        columns = [
            Column('id', meaning=ids[root].implementation + ': ' + type_label(ids[root].type))
        ] + [
            Column('dim_' + b.key, meaning=f'Dimension {b.key}: {type_label(b.type)}')
            for b in dimensions
        ]
        checks: list[str] = []
        if config:
            keys.append('field')
            columns.append(Column('field', meaning='Logical configuration field qualified name'))
        if f.category == 'snapshot':
            keys.append('date')
            columns.append(Column('date', meaning='Exact snapshot date (ISO)'))
        if f.category == 'scd':
            keys.append('valid_from')
            columns.extend(
                [
                    Column('valid_from', meaning='Inclusive start date'),
                    Column('valid_to', meaning='Exclusive end date'),
                ]
            )
            checks.append(
                "'1900-01-01' <= valid_from AND valid_from < valid_to AND valid_to <= '2999-12-31'"
            )
        if name not in tables:
            tables[name] = Table(
                name,
                f'{f.category} records for {root}; dimensions: {", ".join(b.key for b in dimensions) or "none"}',
                columns,
                tuple(keys),
                checks,
            )
        table = tables[name]
        if config:
            state, value = 'state', '' if collection else 'value'
            config_columns(table, f)
        else:
            state, value = f.name + '__state', '' if collection else f.name
            additions = [
                Column(
                    state,
                    default="'NO_ENTRY'",
                    meaning=f'{f.identity}: VALUE, NO_ENTRY, EMPTY or NOT_APPLICABLE',
                )
            ]
            table.checks.append(
                f'{quote(state)} IN (\'VALUE\', \'NO_ENTRY\', \'EMPTY\', \'NOT_APPLICABLE\')'
            )
            if value:
                additions.append(
                    Column(
                        value,
                        'INTEGER' if f.type.name == 'bool' else 'TEXT',
                        True,
                        meaning=f'{f.identity}: {type_label(f.type)}; reference target {f.type.target or "none"}',
                    )
                )
                table.checks.append(
                    f'({quote(state)} = \'VALUE\' AND {quote(value)} IS NOT NULL) OR ({quote(state)} <> \'VALUE\' AND {quote(value)} IS NULL)'
                )
            if set(c.name for c in additions) & set(c.name for c in table.columns):
                raise ValueError(f'{f.identity}: SQL column collision')
            table.columns.extend(additions)
        table.fields.append(f.identity)
        members = member_name if collection else ''
        if members:
            claim(members, f.identity + ':members')
            key_sql = ', '.join(quote(k) for k in keys)
            tables[members] = Table(
                members,
                f'Unordered unique references for {f.identity}',
                [c for c in columns if c.name in keys]
                + [
                    Column(
                        'member_id',
                        meaning=f'Logical reference to {f.type.arguments[0].target}; target existence is not enforced',
                    )
                ],
                (*keys, 'member_id'),
                checks=[f'field = {sql_literal(f.identity)}'] if config else [],
                foreign_keys=[f'FOREIGN KEY ({key_sql}) REFERENCES {quote(name)} ({key_sql})'],
                fields=[f.identity],
            )
        if f.category == 'scd':
            table.indexes.append(tuple([*keys[:-1], 'valid_to']))
        stored[f.identity] = StoredField(
            f,
            root,
            name,
            state,
            value,
            members,
            dimensions,
            (('field', f.identity),) if config else (),
        )
    for root in sorted(ids):
        name = table_name(root) + '_config'
        if claims.get(name) == root + ':config':
            tables[name].checks.append(
                'field IN (' + ', '.join(sql_literal(f) for f in tables[name].fields) + ')'
            )
    for table in builtin_tables(model, stored):
        claim(table.name, table.purpose)
        tables[table.name] = table
    return Layout(model, entities, roots, ids, stored, tuple(tables[k] for k in sorted(tables)))


def config_columns(table: Table, field_: Field) -> None:
    """Add a typed field contract to a shared configuration long table.

    :param table: Table whose key already contains ID and field identity.
    :param field_: Representative resolved field contract.
    :returns: None after adding columns once and a constraint for this field.
    """
    if not table.fields:
        table.columns.extend(
            [
                Column(
                    'state',
                    default="'NO_ENTRY'",
                    meaning='VALUE, NO_ENTRY, EMPTY or NOT_APPLICABLE',
                ),
                Column(
                    'value',
                    'BLOB',
                    True,
                    meaning='No affinity: TEXT scalar/ID payload, INTEGER bool, NULL for collections or non-normal states',
                ),
            ]
        )
        table.checks.append("state IN ('VALUE', 'NO_ENTRY', 'EMPTY', 'NOT_APPLICABLE')")
    if field_.type.name == 'collection':
        constraint = 'value IS NULL'
    else:
        payload = (
            "typeof(value) = 'integer' AND value IN (0, 1)"
            if field_.type.name == 'bool'
            else "typeof(value) = 'text'"
        )
        constraint = f"(state = 'VALUE' AND ({payload})) OR (state <> 'VALUE' AND value IS NULL)"
    table.checks.append(f'field <> {sql_literal(field_.identity)} OR ({constraint})')


def builtin_tables(model: Model, stored: dict[str, StoredField]) -> list[Table]:
    """Store embedded built-in models without enabling their query algorithms.

    :param model: IR containing the exact built-in definitions used by compilation.
    :param stored: Field mappings extended with storage-only built-in fields.
    :returns: Built-in identity and member tables, including declared dictionary rows.
    """
    definitions = {d.identity: d for d in model.definitions}
    tables: list[Table] = []
    for identity in ('builtins.Decodes', 'builtins.Hierarchy', 'builtins.ConfigGroup'):
        definition = definitions[identity]
        id_ = next(f for f in definition.fields if f.category == 'id')
        name = table_name(identity)
        table = Table(
            name,
            f'Built-in identity table for {identity}; storage only',
            [Column(id_.name, meaning=id_.identity + ': string')],
            (id_.name,),
        )
        tables.append(table)
        for f in definition.fields:
            if f.category == 'id':
                continue
            state = f.name + '__state'
            table.columns.append(
                Column(state, default="'NO_ENTRY'", meaning=f'{f.identity}: field state')
            )
            table.checks.append(
                f'{quote(state)} IN (\'VALUE\', \'NO_ENTRY\', \'EMPTY\', \'NOT_APPLICABLE\')'
            )
            table.fields.append(f.identity)
            value, members = '', ''
            if f.type.name == 'type':
                value = f.name
                table.columns.append(
                    Column(value, nullable=True, meaning=f'{f.identity}: qualified entity type')
                )
                table.checks.append(
                    f'({quote(state)} = \'VALUE\' AND {quote(value)} IS NOT NULL) OR ({quote(state)} <> \'VALUE\' AND {quote(value)} IS NULL)'
                )
            else:
                members = (
                    name
                    + '_'
                    + f.name.removeprefix(snake(definition.name.rsplit('.', 1)[-1]) + '_')
                )
                columns = [Column(id_.name, meaning=f'Owner: {identity}')]
                checks: list[str] = []
                if f.type.name == 'values':
                    entry_definition = definitions[f.type.arguments[0].target]
                    columns.extend(
                        Column(e.name, meaning=e.identity + ': ' + type_label(e.type))
                        for e in entry_definition.fields
                    )
                    key = 'code' if identity == 'builtins.Decodes' else 'ordering'
                else:
                    key = 'field'
                    columns.extend(
                        [
                            Column(key, meaning='Logical field qualified name'),
                            Column(
                                'value',
                                meaning='JSON object: type (resolved IR Type), value (payload); no configuration execution',
                            ),
                        ]
                    )
                    checks.append(
                        "CASE WHEN json_valid(value) THEN json_type(value) = 'object' "
                        "AND json_type(value, '$.type') IS 'object' "
                        "AND json_type(value, '$.value') IS NOT NULL ELSE 0 END"
                    )
                tables.append(
                    Table(
                        members,
                        f'Unordered members for {f.identity}; storage only',
                        columns,
                        (id_.name, key),
                        checks,
                        [
                            f'FOREIGN KEY ({quote(id_.name)}) REFERENCES {quote(name)} ({quote(id_.name)})'
                        ],
                        fields=[f.identity],
                    )
                )
            stored[f.identity] = StoredField(f, identity, name, state, value, members, ())
    dictionary = next(t for t in tables if t.name == 'tb_decodes')
    entries = next(t for t in tables if t.name == 'tb_decodes_entries')
    for definition in model.definitions:
        if definition.kind == 'decodes':
            dictionary.rows.append((definition.identity, 'VALUE'))
            for entry in definition.entries:
                values = {
                    'decodes_id': definition.identity,
                    'code': entry.code,
                    'decode': entry.decode,
                    'ordering': entry.ordering,
                }
                entries.rows.append(tuple(values[c.name] for c in entries.columns))
    return tables
