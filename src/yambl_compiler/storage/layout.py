"""Build one physical model shared by SQL, documentation and readers."""

import json
import keyword

from yambl_compiler.ir.codec import encode
from yambl_compiler.ir.model import Definition, Field, Model, Type
from yambl_compiler.storage.records import (
    LAYOUT_REVISION as LAYOUT_REVISION,
)
from yambl_compiler.storage.records import (
    Column as Column,
)
from yambl_compiler.storage.records import (
    Layout as Layout,
)
from yambl_compiler.storage.records import (
    StoredField as StoredField,
)
from yambl_compiler.storage.records import (
    Table as Table,
)
from yambl_compiler.storage.records import (
    quote as quote,
)
from yambl_compiler.storage.records import (
    snake as snake,
)
from yambl_compiler.storage.records import (
    sql_literal as sql_literal,
)
from yambl_compiler.storage.records import (
    table_name as table_name,
)


def type_label(type_: Type) -> str:
    """Describe a supported resolved type for the schema reference.

    :param type_: Resolved field or dimension type.
    :returns: Qualified type with generic arguments, parameters and exact constraint.
    """
    text = type_.target or type_.name
    if type_.arguments:
        text += "<" + ", ".join(type_label(arg) for arg in type_.arguments) + ">"
    if type_.parameters:
        text += "[" + ", ".join(p.name + ": " + p.value for p in type_.parameters) + "]"
    if type_.exact is not None:
        exact = type_.exact
        text += " = " + (
            f'{exact.target}[{" + ".join(exact.selections) or "None"}; mask={exact.mask}]'
            if exact.kind == "flags"
            else exact.text
        )
    return text


def supported(type_: Type, entities: dict[str, Definition], model: Model) -> bool:
    """Check the first backend's supported storage types.

    :param type_: Resolved type.
    :param entities: Business entity definitions.
    :param model: Embedded dictionary definitions.
    :returns: Whether the type has implemented storage and read semantics.
    """
    if type_.dependent:
        return False
    if type_.kind == "reference":
        return type_.target in entities
    if type_.name == "collection":
        return len(type_.arguments) == 1 and type_.arguments[0].target in entities
    if type_.name == "code":
        target = type_.arguments[0].target
        return not any(
            d.identity == target and any(c.target == "builtins.flags" for c in d.decorators)
            for d in model.definitions
        )
    return not type_.target and type_.name in (
        "string",
        "bool",
        "integer",
        "snowflake_id",
        "decimal",
        "date",
    )


def build_layout(model: Model) -> Layout:
    """Preflight supported business fields and construct deterministic tables.

    :param model: Semantically validated IR.
    :returns: Shared physical model.
    :raises ValueError: On unsupported types, IDs or generated-name collisions.
    """
    for definition in model.definitions:
        if (
            definition.kind == "entity"
            and definition.source.origin == "builtin"
            and definition.identity
            not in ("builtins.Decodes", "builtins.Hierarchy", "builtins.ConfigGroup")
            and definition.lookups
        ):
            location = definition.lookups[0].source
            raise ValueError(
                f"{location.path}:{location.line}:{location.column}: {definition.identity}: "
                "builtin entity storage is not supported"
            )
    entities = {
        d.identity: d
        for d in model.definitions
        if d.kind == "entity" and d.source.origin == "model"
    }
    declarations = {f.implementation: f for d in model.definitions for f in d.fields}
    roots: dict[str, str] = {}
    ids: dict[str, Field] = {}
    names: set[str] = set()
    for key, entity in entities.items():
        short = entity.name.rsplit(".", 1)[-1]
        method = snake(short)
        if short in names or method in names or keyword.iskeyword(method):
            raise ValueError(f"{key}: Python name collision")
        names.update((short, method))
        root_definition = entity
        while root_definition.parent in entities:
            root_definition = entities[root_definition.parent]
        roots[key] = root_definition.identity
        effective_ids = [
            declarations[e.implementation]
            for e in entity.effective
            if declarations[e.implementation].category == "id"
        ]
        if len(effective_ids) != 1 or effective_ids[0].type.name not in (
            "string",
            "integer",
            "snowflake_id",
        ):
            raise ValueError(f"{key}: requires exactly one string or integer ID")
        id_ = effective_ids[0]
        if (
            root_definition.identity in ids
            and ids[root_definition.identity].implementation != id_.implementation
        ):
            raise ValueError(f"{key}: an inheritance tree must share its root ID")
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
            raise ValueError(f"{owner}: SQL table collision with {claims[name]}: {name}")
        claims[name] = owner

    metadata = Table(
        "tb_yambl_metadata",
        "Model identity",
        [Column("key"), Column("value")],
        ("key",),
    )
    tables[metadata.name] = metadata
    claim(metadata.name, "storage metadata")
    for root, id_ in sorted(ids.items()):
        name = table_name(root)
        claim(name, root)
        tables[name] = Table(
            name,
            f"Identity table for {root}",
            [
                Column("id", meaning=id_.implementation + ": " + type_label(id_.type)),
                Column("entity_type", meaning="Actual qualified entity type"),
            ],
            ("id",),
            [
                f'{quote("entity_type")} IN ({", ".join(sql_literal(k) for k in roots if roots[k] == root)})'
            ],
        )
    stored: dict[str, StoredField] = {}
    groups: dict[tuple[str, str, str], str] = {}
    sparse_tables: set[str] = set()
    for logical in model.fields:
        impls = [declarations[i] for i in logical.implementations]
        f = impls[0]
        owner = f.implementation.rsplit(".", 1)[0]
        if owner not in entities or f.builtin or f.category in ("id", "computed"):
            continue
        dimensions = tuple(
            sorted(
                (
                    b
                    for c in f.decorators
                    if c.target == "builtins.dimension"
                    for b in c.bound
                    if b.name == "variation"
                ),
                key=lambda b: b.key,
            )
        )
        for implementation in impls:
            if not supported(implementation.type, entities, model):
                s = implementation.source
                raise ValueError(
                    f"{s.path}:{s.line}:{s.column}: {implementation.implementation}: unsupported storage type {implementation.type.name}"
                )
        if any(not supported(b.type, entities, model) for b in dimensions):
            raise ValueError(f"{f.identity}: unsupported dimension type")
        if (
            keyword.iskeyword(f.name)
            or f.name in ("id", "entity_type")
            or any(b.key == "self" or keyword.iskeyword(b.key) for b in dimensions)
        ):
            raise ValueError(f"{f.identity}: generated Python parameter or field collision")
        root = roots[owner]
        placements = {
            (c.target.rsplit(".", 1)[-1], c.bound[0].type.exact.text)
            for implementation in impls
            for c in implementation.decorators
            if c.target in ("builtins.sparse", "builtins.dense")
            and c.bound[0].type.exact is not None
        }
        if len(placements) > 1:
            raise ValueError(
                f"{f.identity}: conflicting storage groups across field implementations"
            )
        placement, category = next(iter(placements), ("", ""))
        sparse = placement == "sparse"
        signature = json.dumps(
            [{"key": b.key, "type": encode(b.type)} for b in dimensions], sort_keys=True
        )
        collection = f.type.name == "collection"
        config = f.category == "config"
        base = table_name(root)
        member_name = base + "_" + f.name
        if placement:
            name, owner_key = (
                base + "_" + category,
                root + ":" + placement + ":" + category,
            )
        elif config:
            name, owner_key = base + "_config", root + ":config"
        elif f.category == "static" and not dimensions:
            name, owner_key = base, root
        elif f.category in ("snapshot", "scd") or collection:
            name = member_name + ("_status" if collection else "")
            owner_key = f.identity
        else:
            group_key = (root, f.category, signature)
            if group_key not in groups:
                group_base = base + "_" + f.category + "_" + "_".join(b.key for b in dimensions)
                name = group_base
                index = 2
                while name in groups.values():
                    name = group_base + "_" + str(index)
                    index += 1
                groups[group_key] = name
            name = groups[group_key]
            owner_key = repr(group_key)
        claim(name, owner_key)
        keys = ["id", *("dim_" + b.key for b in dimensions)]
        columns = [
            Column(
                "id",
                meaning=ids[root].implementation + ": " + type_label(ids[root].type),
            )
        ] + [
            Column("dim_" + b.key, meaning=f"Dimension {b.key}: {type_label(b.type)}")
            for b in dimensions
        ]
        checks: list[str] = []
        if config or sparse:
            keys.append("field")
            columns.append(Column("field", meaning="Logical field qualified name"))
        if f.category == "snapshot":
            keys.append("date")
            columns.append(Column("date", meaning="Exact snapshot date (ISO)"))
        if f.category == "scd":
            keys.append("valid_from")
            columns.extend(
                [
                    Column("valid_from", meaning="Inclusive start date"),
                    Column("valid_to", meaning="Exclusive end date"),
                ]
            )
            checks.append(
                "'1900-01-01' <= valid_from AND valid_from < valid_to AND valid_to <= '2999-12-31'"
            )
        if name not in tables:
            tables[name] = Table(
                name,
                f'{placement or f.category} records for {root}; dimensions: {", ".join(b.key for b in dimensions) or "none"}',
                columns,
                tuple(keys),
                checks,
            )
        table = tables[name]
        if sparse or config:
            sparse_tables.add(name)
            state = "state"
            type_ = ids[roots[f.type.target]].type if f.type.kind == "reference" else f.type
            value = (
                ""
                if collection
                else {"code": "string", "snowflake_id": "int", "integer": "int"}.get(
                    type_.name, type_.name
                )
            )
            sparse_columns(table, f, value)
        else:
            state, value = f.name + "__state", "" if collection else f.name
            additions = [
                Column(
                    state,
                    default="'NO_ENTRY'",
                    meaning=f"{f.identity}: VALUE, NO_ENTRY, EMPTY or NOT_APPLICABLE",
                )
            ]
            table.checks.append(
                f"{quote(state)} IN ('VALUE', 'NO_ENTRY', 'EMPTY', 'NOT_APPLICABLE')"
            )
            if value:
                additions.append(
                    Column(
                        value,
                        "INTEGER" if f.type.name == "bool" else "TEXT",
                        True,
                        meaning=f'{f.identity}: {type_label(f.type)}; reference target {f.type.target or "none"}',
                    )
                )
                table.checks.append(
                    f"({quote(state)} = 'VALUE' AND {quote(value)} IS NOT NULL) OR ({quote(state)} <> 'VALUE' AND {quote(value)} IS NULL)"
                )
            if set(c.name for c in additions) & set(c.name for c in table.columns):
                raise ValueError(f"{f.identity}: SQL column collision")
            table.columns.extend(additions)
        table.fields.append(f.identity)
        members = member_name if collection else ""
        if members:
            claim(members, f.identity + ":members")
            key_sql = ", ".join(quote(k) for k in keys)
            tables[members] = Table(
                members,
                f"Unordered unique references for {f.identity}",
                [c for c in columns if c.name in keys]
                + [
                    Column(
                        "member_id",
                        meaning=f"Logical reference to {f.type.arguments[0].target}; target existence is not enforced",
                    )
                ],
                (*keys, "member_id"),
                checks=[f"field = {sql_literal(f.identity)}"] if config else [],
                foreign_keys=[f"FOREIGN KEY ({key_sql}) REFERENCES {quote(name)} ({key_sql})"],
                parents=[name],
                fields=[f.identity],
            )
        if f.category == "scd":
            table.indexes.append(tuple([*keys[:-1], "valid_to"]))
        stored[f.identity] = StoredField(
            f,
            root,
            name,
            state,
            value,
            members,
            dimensions,
            (("field", f.identity),) if config or sparse else (),
        )
    for name in sorted(sparse_tables):
        table = tables[name]
        payloads = [c.name for c in table.columns if c.name not in ("id", "field", "state")]
        table.checks.append("field IN (" + ", ".join(sql_literal(f) for f in table.fields) + ")")
        for identity in table.fields:
            value = stored[identity].value
            others = [f"{quote(p)} IS NULL" for p in payloads if p != value]
            if others:
                table.checks.append(
                    f"field <> {sql_literal(identity)} OR (" + " AND ".join(others) + ")"
                )
    for table in builtin_tables(model, stored):
        claim(table.name, table.purpose)
        tables[table.name] = table
    for definition in model.definitions:
        if definition.identity in (
            "builtins.Decodes",
            "builtins.Hierarchy",
            "builtins.ConfigGroup",
        ):
            entities[definition.identity] = definition
            roots[definition.identity] = definition.identity
            ids[definition.identity] = next(f for f in definition.fields if f.category == "id")
    return Layout(model, entities, roots, ids, stored, tuple(tables[k] for k in sorted(tables)))


def sparse_columns(table: Table, field_: Field, value: str) -> None:
    """Add a scalar lane and enforce the long row's state and payload type.

    :param table: Sparse group with ID and logical field keys.
    :param field_: Resolved logical field implementation.
    :param value: Payload lane selected by the scalar or reference ID type.
    :returns: None after adding the lane and its field-specific constraint.
    """
    if not table.fields:
        table.columns.append(
            Column(
                "state",
                default="'NO_ENTRY'",
                meaning="VALUE, NO_ENTRY, EMPTY or NOT_APPLICABLE",
            )
        )
        table.checks.append("state IN ('VALUE', 'NO_ENTRY', 'EMPTY', 'NOT_APPLICABLE')")
    if not value:
        return
    if value not in {c.name for c in table.columns}:
        table.columns.append(
            Column(
                value,
                "INTEGER" if value == "bool" else "TEXT",
                True,
                meaning=f"{value} payload; exactly one lane for VALUE, all NULL otherwise",
            )
        )
    payload = (
        f"typeof({quote(value)}) = 'integer' AND {quote(value)} IN (0, 1)"
        if value == "bool"
        else f"typeof({quote(value)}) = 'text'"
    )
    table.checks.append(
        f"field <> {sql_literal(field_.identity)} OR ((state = 'VALUE' AND ({payload})) OR (state <> 'VALUE' AND {quote(value)} IS NULL))"
    )


def builtin_tables(model: Model, stored: dict[str, StoredField]) -> list[Table]:
    """Store embedded built-in models without enabling their query algorithms.

    :param model: IR containing the exact built-in definitions used by compilation.
    :param stored: Field mappings extended with storage-only built-in fields.
    :returns: Built-in identity and member tables, including declared dictionary rows.
    """
    definitions = {d.identity: d for d in model.definitions}
    tables: list[Table] = []
    supported_fields = {
        "builtins.Decodes": {"entries"},
        "builtins.Hierarchy": {"hierarchy_entries"},
        "builtins.ConfigGroup": {"priority", "target_entity_type", "when", "then_set"},
    }
    for identity in ("builtins.Decodes", "builtins.Hierarchy", "builtins.ConfigGroup"):
        definition = definitions[identity]
        id_ = next(f for f in definition.fields if f.category == "id")
        name = table_name(identity)
        table = Table(
            name,
            f"Built-in identity table for {identity}; storage only",
            [Column(id_.name, meaning=id_.identity + ": string")],
            (id_.name,),
        )
        tables.append(table)
        for f in definition.fields:
            if f.category == "id" or f.builtin:
                continue
            if f.category != "static" or any(
                call.target == "builtins.dimension" for call in f.decorators
            ):
                location = f.source
                raise ValueError(
                    f"{location.path}:{location.line}:{location.column}: {f.implementation}: "
                    "builtin storage does not support temporal, config, computed or dimensioned fields"
                )
            if f.name not in supported_fields[identity]:
                location = f.source
                raise ValueError(
                    f"{location.path}:{location.line}:{location.column}: {f.implementation}: "
                    "builtin storage does not support additional fields"
                )
            if f.type.name not in ("type", "integer", "values", "mapping"):
                location = f.source
                raise ValueError(
                    f"{location.path}:{location.line}:{location.column}: {f.implementation}: "
                    f"builtin storage does not support field type {f.type.name}"
                )
            state = f.name + "__state"
            table.columns.append(
                Column(state, default="'NO_ENTRY'", meaning=f"{f.identity}: field state")
            )
            table.checks.append(
                f"{quote(state)} IN ('VALUE', 'NO_ENTRY', 'EMPTY', 'NOT_APPLICABLE')"
            )
            table.fields.append(f.identity)
            value, members = "", ""
            if f.type.name in ("type", "integer"):
                value = f.name
                table.columns.append(
                    Column(
                        value,
                        nullable=True,
                        meaning=f"{f.identity}: {type_label(f.type)}",
                    )
                )
                table.checks.append(
                    f"({quote(state)} = 'VALUE' AND {quote(value)} IS NOT NULL) OR ({quote(state)} <> 'VALUE' AND {quote(value)} IS NULL)"
                )
            else:
                members = (
                    name
                    + "_"
                    + f.name.removeprefix(snake(definition.name.rsplit(".", 1)[-1]) + "_")
                )
                columns = [Column(id_.name, meaning=f"Owner: {identity}")]
                checks: list[str] = []
                if f.type.name == "values":
                    entry_definition = definitions[f.type.arguments[0].target]
                    columns.extend(
                        Column(e.name, meaning=e.identity + ": " + type_label(e.type))
                        for e in entry_definition.fields
                    )
                    key = "code" if identity == "builtins.Decodes" else "ordering"
                else:
                    key = "field"
                    columns.extend(
                        [
                            Column(key, meaning="Logical field qualified name"),
                            Column(
                                "value",
                                meaning="JSON object: type (resolved IR Type), value (payload); no configuration execution",
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
                        f"Unordered members for {f.identity}; storage only",
                        columns,
                        (id_.name, key),
                        checks,
                        [
                            f"FOREIGN KEY ({quote(id_.name)}) REFERENCES {quote(name)} ({quote(id_.name)})"
                        ],
                        fields=[f.identity],
                        parents=[name],
                    )
                )
            stored[f.identity] = StoredField(f, identity, name, state, value, members, ())
    dictionary = next(t for t in tables if t.name == "tb_decodes")
    entries = next(t for t in tables if t.name == "tb_decodes_entries")
    for definition in model.definitions:
        if definition.kind == "decodes":
            dictionary.rows.append((definition.identity, "VALUE"))
            for entry in definition.entries:
                values = {
                    "decodes_id": definition.identity,
                    "code": entry.code,
                    "decode": entry.decode,
                    "ordering": entry.ordering,
                }
                entries.rows.append(tuple(values[c.name] for c in entries.columns))
    return tables
