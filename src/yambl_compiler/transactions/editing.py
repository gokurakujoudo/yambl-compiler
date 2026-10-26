"""Edit detached physical records through model-bound business data images."""

import json
from dataclasses import asdict
from datetime import date

from yambl_compiler.ir.model import Field
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol, Value
from yambl_compiler.languages.shared.writing import literal
from yambl_compiler.languages.yatran.model import Image
from yambl_compiler.storage.backends import Row, validate_row
from yambl_compiler.storage.contract import loads
from yambl_compiler.storage.records import Layout, StoredField
from yambl_compiler.transactions.binding import Catalog, Entity, object_items

STATES = ('NO_ENTRY', 'EMPTY', 'NOT_APPLICABLE')


class Data:
    """Own one detached candidate snapshot; never publish changes directly."""

    def __init__(self, layout: Layout, tables: dict[str, list[Row]]) -> None:
        """Copy table records so candidate failures cannot mutate the source.

        :param layout: Validated storage layout.
        :param tables: Complete physical table contents.
        :returns: None.
        """
        self.catalog = Catalog(layout)
        self.layout = layout
        self.tables = {name: [dict(row) for row in rows] for name, rows in tables.items()}
        self.schema = {t.name: t for t in layout.tables}

    def blank(self, table: str, values: Row) -> Row:
        """Fill physical defaults for a newly created record.

        :param table: Trusted physical table name.
        :param values: Explicit columns.
        :returns: Complete physical row.
        """
        return {
            c.name: values.get(c.name, 'NO_ENTRY' if c.default == "'NO_ENTRY'" else None)
            for c in self.schema[table].columns
        }

    def actual(self, entity: Entity, id_: Value) -> Entity:
        """Resolve the actual subtype of an existing ID.

        :param entity: Requested entity or ancestor.
        :param id_: Business ID literal.
        :returns: Actual entity binding.
        :raises ValueError: On absent IDs or incompatible actual types.
        """
        encoded = self.catalog.scalar(entity.id.type, id_)
        row = next((r for r in self.tables[entity.table] if r[entity.column] == encoded), None)
        if row is None:
            raise ValueError(f'entity does not exist: {entity.definition.identity} {literal(id_)}')
        if entity.root not in self.layout.entities:
            return entity
        actual = self.catalog.entity(str(row['entity_type']))
        ancestor = actual.definition
        while (
            ancestor.identity != entity.definition.identity
            and ancestor.parent in self.catalog.definitions
        ):
            ancestor = self.catalog.definitions[ancestor.parent]
        if ancestor.identity != entity.definition.identity:
            raise ValueError('ID belongs to an incompatible entity type')
        return actual

    def insert(self, entity: Entity, id_: Value) -> None:
        """Insert a new identity with all unspecified fields in NO_ENTRY.

        :param entity: Actual entity type to create.
        :param id_: Business ID literal.
        :returns: None.
        :raises ValueError: If the inheritance root already contains this ID.
        """
        encoded = self.catalog.scalar(entity.id.type, id_)
        if any(r[entity.column] == encoded for r in self.tables[entity.table]):
            raise ValueError('entity ID already exists')
        values: Row = {entity.column: encoded}
        if entity.root in self.layout.entities:
            values['entity_type'] = entity.definition.identity
        self.tables[entity.table].append(self.blank(entity.table, values))

    def coordinates(self, entity: Entity, field: Field, stored: StoredField) -> dict[str, str]:
        """Map public row coordinates to physical columns.

        :param entity: Field owner binding.
        :param field: Effective field.
        :param stored: Physical field mapping.
        :returns: Ordered public-to-physical coordinates.
        """
        result = {entity.id.name: entity.column}
        result.update((d.key, 'dim_' + d.key) for d in stored.dimensions)
        if field.category == 'snapshot':
            result[self.time_name(field)] = 'date'
        elif field.category == 'scd':
            result.update(valid_from='valid_from', valid_to='valid_to')
        return result

    def time_name(self, field: Field) -> str:
        """Read the declared temporal parameter name.

        :param field: Temporal field.
        :returns: Model-defined name.
        """
        return next(
            b.key
            for d in field.decorators
            if d.target == 'builtins.temporal'
            for b in d.bound
            if b.key
        )

    def selection(
        self, entity: Entity, field: Field, stored: StoredField, conditions: tuple[Condition, ...]
    ) -> tuple[Row, tuple[str, str] | None]:
        """Bind a scope before accessing candidate records.

        :param entity: Requested owner.
        :param field: Selected field.
        :param stored: Physical field mapping.
        :param conditions: Public scope conditions.
        :returns: Equality filters and optional time range.
        :raises ValueError: On missing owner, invalid coordinates or ranges.
        """
        filters: Row = dict(stored.key_values)
        types = {
            entity.id.name: (entity.column, entity.id.type),
            **{d.key: ('dim_' + d.key, d.type) for d in stored.dimensions},
        }
        temporal = self.time_name(field) if field.category in ('snapshot', 'scd') else ''
        interval: tuple[str, str] | None = None
        for condition in conditions:
            if condition.name == temporal:
                if not isinstance(condition.value, str) or not isinstance(condition.end, str):
                    raise ValueError('temporal scope requires a half-open date range')
                start, end = condition.value, condition.end
                if (
                    date.fromisoformat(start).isoformat() != start
                    or date.fromisoformat(end).isoformat() != end
                    or start >= end
                ):
                    raise ValueError('invalid temporal scope')
                interval = start, end
            elif condition.name in types and condition.end is None:
                column, type_ = types[condition.name]
                filters[column] = self.catalog.scalar(type_, condition.value)
            else:
                raise ValueError(f'unknown or invalid scope coordinate: {condition.name}')
        if entity.column not in filters:
            raise ValueError('scope requires owner ID')
        return filters, interval

    def matches(
        self, row: Row, filters: Row, interval: tuple[str, str] | None, category: str
    ) -> bool:
        """Match equality keys and intersecting temporal coverage.

        :param row: Physical row.
        :param filters: Bound equality filters.
        :param interval: Optional date interval.
        :param category: Attribute category.
        :returns: Whether the row belongs to the scope.
        """
        if any(row.get(k) != v for k, v in filters.items()):
            return False
        if interval is None:
            return True
        start, end = interval
        return (
            start <= str(row['date']) < end
            if category == 'snapshot'
            else str(row['valid_from']) < end and str(row['valid_to']) > start
        )

    def payload(self, entity: Entity, field: Field, stored: StoredField, row: Row) -> Value:
        """Decode one field value without applying effective defaults.

        :param entity: Actual owner.
        :param field: Effective field contract.
        :param stored: Storage mapping.
        :param row: Located owner record.
        :returns: Data literal, collection or stored state.
        """
        state = str(row[stored.state])
        if state in STATES:
            return Symbol(state)
        if state != 'VALUE':
            raise ValueError('invalid stored state')
        if stored.value:
            return self.catalog.literal(field.type, row[stored.value])
        keys = self.schema[stored.members].primary[:-1]
        members = [r for r in self.tables[stored.members] if all(r[k] == row[k] for k in keys)]
        if field.type.name == 'collection':
            values = [
                self.catalog.literal(field.type.arguments[0], r['member_id']) for r in members
            ]
            return tuple(sorted(values, key=literal))
        if field.type.name == 'values':
            definition = self.catalog.definitions[field.type.arguments[0].target]
            return tuple(
                ObjectValue(
                    tuple(
                        (Symbol(f.name), self.catalog.literal(f.type, member[f.name]))
                        for f in definition.fields
                    )
                )
                for member in sorted(
                    members, key=lambda r: str(r[self.schema[stored.members].primary[-1]])
                )
            )
        entries: list[tuple[Value, Value]] = []
        for member in sorted(members, key=lambda r: str(r['field'])):
            identity = str(member['field'])
            type_ = self.layout.fields[identity].field.type
            envelope = loads(str(member['value']))
            if not isinstance(envelope, dict) or 'value' not in envelope:
                raise ValueError('invalid configuration mapping payload')
            raw = envelope['value']
            if type_.name == 'collection' and not isinstance(raw, list):
                raise ValueError('invalid configuration collection')
            value = (
                tuple(self.catalog.literal(type_.arguments[0], v) for v in raw)
                if isinstance(raw, list)
                else self.catalog.literal(type_, raw)
            )
            entries.append((Symbol(identity), value))
        return ObjectValue(tuple(entries))

    def image(self, target: str, conditions: tuple[Condition, ...]) -> Image:
        """Capture scope data using business column names and clipped SCD intervals.

        :param target: Qualified field target.
        :param conditions: Boundable scope conditions.
        :returns: Portable data image.
        """
        entity, field, stored = self.catalog.target(target)
        if field is None or stored is None:
            raise ValueError('field image requires a field target')
        filters, interval = self.selection(entity, field, stored, conditions)
        coords = self.coordinates(entity, field, stored)
        rows: list[tuple[Value, ...]] = []
        dimensions = {d.key: d.type for d in stored.dimensions}
        for physical in self.tables[stored.table]:
            if not self.matches(physical, filters, interval, field.category):
                continue
            row = dict(physical)
            if interval is not None and field.category == 'scd':
                row['valid_from'] = max(str(row['valid_from']), interval[0])
                row['valid_to'] = min(str(row['valid_to']), interval[1])
            values: list[Value] = []
            for name, column in coords.items():
                if name == entity.id.name:
                    value = self.catalog.literal(entity.id.type, row[column])
                elif name in dimensions:
                    value = self.catalog.literal(dimensions[name], row[column])
                else:
                    value = str(row[column])
                values.append(value)
            rows.append((*values, self.payload(entity, field, stored, physical)))
        return Image(
            target,
            (*coords, 'value'),
            tuple(sorted(rows, key=lambda r: tuple(literal(v) for v in r))),
        )

    def member_rows(self, stored: StoredField, owner: Row) -> list[Row]:
        """Copy the collection records belonging to one physical field row.

        :param stored: Field storage binding.
        :param owner: Physical owner record.
        :returns: Detached matching member rows.
        """
        if not stored.members:
            return []
        keys = self.schema[stored.members].primary[:-1]
        return [dict(r) for r in self.tables[stored.members] if all(r[k] == owner[k] for k in keys)]

    def erase(self, entity: Entity, stored: StoredField, row: Row) -> None:
        """Remove a field row while retaining unrelated shared-table fields.

        :param entity: Owner binding.
        :param stored: Field storage.
        :param row: Located physical row.
        :returns: None.
        """
        if stored.members:
            keys = self.schema[stored.members].primary[:-1]
            self.tables[stored.members] = [
                r for r in self.tables[stored.members] if not all(r[k] == row[k] for k in keys)
            ]
        if stored.table != entity.table and (
            stored.key_values or len(self.schema[stored.table].fields) == 1
        ):
            self.tables[stored.table].remove(row)
        else:
            row[stored.state] = 'NO_ENTRY'
            if stored.value:
                row[stored.value] = None

    def clear(self, target: str, conditions: tuple[Condition, ...]) -> None:
        """Delete a field scope, splitting SCD records at its boundaries.

        :param target: Qualified field.
        :param conditions: Public scope coordinates.
        :returns: None.
        """
        entity, field, stored = self.catalog.target(target)
        if field is None or stored is None:
            raise ValueError('scope deletion requires a field')
        filters, interval = self.selection(entity, field, stored, conditions)
        for row in list(self.tables[stored.table]):
            if not self.matches(row, filters, interval, field.category):
                continue
            members = self.member_rows(stored, row)
            fragments: list[Row] = []
            if field.category == 'scd' and interval is not None:
                if str(row['valid_from']) < interval[0]:
                    fragments.append({**row, 'valid_to': interval[0]})
                if str(row['valid_to']) > interval[1]:
                    fragments.append({**row, 'valid_from': interval[1]})
            self.erase(entity, stored, row)
            for fragment in fragments:
                self.tables[stored.table].append(fragment)
                if stored.members:
                    self.tables[stored.members].extend(
                        {**member, 'valid_from': fragment['valid_from']} for member in members
                    )

    def encode_members(
        self, entity: Entity, field: Field, stored: StoredField, row: Row, value: Value
    ) -> list[Row]:
        """Encode supported reference collections, built-in entries and mappings.

        :param entity: Actual owner.
        :param field: Field contract.
        :param stored: Physical binding.
        :param row: Complete physical owner row.
        :param value: Business literal.
        :returns: Physical member rows.
        :raises ValueError: On invalid member shape or constraints.
        """
        keys = self.schema[stored.members].primary[:-1]
        common = {k: row[k] for k in keys}
        if field.type.name == 'collection':
            if not isinstance(value, tuple):
                raise ValueError('expected collection literal')
            return [
                {**common, 'member_id': self.catalog.scalar(field.type.arguments[0], v)}
                for v in value
            ]
        if field.type.name == 'values':
            if not isinstance(value, tuple):
                raise ValueError('expected values literal')
            definition = self.catalog.definitions[field.type.arguments[0].target]
            result: list[Row] = []
            for member in value:
                items = object_items(member)
                if set(items) != {f.name for f in definition.fields}:
                    raise ValueError('missing or unknown entry field')
                result.append(
                    {
                        **common,
                        **{
                            f.name: self.catalog.scalar(f.type, items[f.name])
                            for f in definition.fields
                        },
                    }
                )
            return result
        result = []
        for identity, item in object_items(value).items():
            target = self.layout.fields.get(identity)
            if target is None or target.field.category != (
                'static' if field.name == 'when' else 'config'
            ):
                raise ValueError(
                    'ConfigGroup mapping requires static conditions or config assignments'
                )
            type_ = target.field.type
            if type_.name == 'collection':
                if not isinstance(item, tuple):
                    raise ValueError('configuration collection requires a collection literal')
                payload: object = [self.catalog.scalar(type_.arguments[0], v) for v in item]
            else:
                payload = self.catalog.scalar(type_, item)
            result.append(
                {
                    **common,
                    'field': identity,
                    'value': json.dumps(
                        {'type': asdict(type_), 'value': payload},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(',', ':'),
                    ),
                }
            )
        return result

    def put(self, target: str, conditions: tuple[Condition, ...], image: Image) -> None:
        """Replace a scope from business records, preserving outside SCD coverage.

        :param target: Qualified field.
        :param conditions: Replacement scope.
        :param image: Target business records with declared columns.
        :returns: None.
        :raises ValueError: On invalid columns, out-of-scope rows or payloads.
        """
        entity, field, stored = self.catalog.target(target)
        if field is None or stored is None:
            raise ValueError('replacement requires a field')
        filters, interval = self.selection(entity, field, stored, conditions)
        coords = self.coordinates(entity, field, stored)
        constants = {c.name: c.value for c in conditions if c.end is None}
        expected = set(coords) | {'value'}
        if set(image.columns) - expected or expected - set(image.columns) - constants.keys():
            raise ValueError('replacement columns must provide all coordinates and value')
        dimensions = {d.key: d.type for d in stored.dimensions}
        additions: list[tuple[Row, list[Row]]] = []
        seen: set[tuple[object, ...]] = set()
        for values in image.rows:
            data = {**constants, **dict(zip(image.columns, values, strict=True))}
            row: Row = dict(stored.key_values)
            actual = self.actual(entity, data[entity.id.name])
            actual_field, _ = self.catalog.field(actual, field.name)
            for name, column in coords.items():
                if name == entity.id.name:
                    row[column] = self.catalog.scalar(entity.id.type, data[name])
                elif name in dimensions:
                    row[column] = self.catalog.scalar(dimensions[name], data[name])
                else:
                    raw = data[name]
                    if not isinstance(raw, str) or date.fromisoformat(raw).isoformat() != raw:
                        raise ValueError('expected ISO temporal coordinate')
                    row[column] = raw
            if not self.matches(row, filters, interval, field.category):
                raise ValueError('replacement record is outside scope')
            if (
                field.category == 'scd'
                and interval is not None
                and not (
                    interval[0] <= str(row['valid_from']) < str(row['valid_to']) <= interval[1]
                )
            ):
                raise ValueError('replacement interval crosses scope boundary')
            value = data['value']
            state = value.name if isinstance(value, Symbol) and value.name in STATES else 'VALUE'
            row[stored.state] = state
            if stored.value:
                row[stored.value] = (
                    self.catalog.scalar(actual_field.type, value) if state == 'VALUE' else None
                )
            members = (
                self.encode_members(actual, actual_field, stored, row, value)
                if stored.members and state == 'VALUE'
                else []
            )
            key = tuple(row[k] for k in self.schema[stored.table].primary)
            if key in seen:
                raise ValueError('duplicate replacement coordinate')
            seen.add(key)
            additions.append((row, members))
        self.clear(target, conditions)
        for row, members in additions:
            keys = self.schema[stored.table].primary
            existing = next(
                (r for r in self.tables[stored.table] if all(r[k] == row[k] for k in keys)), None
            )
            if existing is None:
                self.tables[stored.table].append(self.blank(stored.table, row))
            else:
                existing.update(row)
            if stored.members:
                self.tables[stored.members].extend(members)

    def entity_images(self, entity: Entity, id_: Value) -> tuple[Image, ...]:
        """Capture an entity identity and every owned field without defaults.

        :param entity: Actual entity binding.
        :param id_: Business ID.
        :returns: Complete business images for deletion or insertion rollback.
        """
        actual = self.actual(entity, id_)
        conditions = (Condition(actual.id.name, id_),)
        result = [Image(actual.definition.identity, (actual.id.name,), ((id_,),))]
        for field in self.catalog.fields(actual).values():
            if field.identity in self.layout.fields:
                result.append(self.image(actual.definition.identity + '.' + field.name, conditions))
        return tuple(result)

    def delete(self, entity: Entity, id_: Value, owned_data: bool) -> None:
        """Delete an identity after explicitly handling its owned records.

        :param entity: Actual entity binding.
        :param id_: Business ID.
        :param owned_data: Explicit authorization to clear owned data.
        :returns: None.
        :raises ValueError: If owned data exists without the marker.
        """
        images = self.entity_images(entity, id_)
        if not owned_data:
            for image in images[1:]:
                _, _, stored = self.catalog.target(image.target)
                assert stored is not None
                if image.rows and (
                    stored.table != entity.table
                    or any(row[-1] != Symbol('NO_ENTRY') for row in image.rows)
                ):
                    raise ValueError('entity deletion requires owned_data delete_all')
        for image in images[1:]:
            self.clear(image.target, (Condition(entity.id.name, id_),))
        encoded = self.catalog.scalar(entity.id.type, id_)
        self.tables[entity.table] = [
            r for r in self.tables[entity.table] if r[entity.column] != encoded
        ]

    def validate(self) -> None:
        """Validate physical record shapes, unique keys and owned-row references.

        :returns: None.
        :raises ValueError: On an invalid candidate snapshot.
        """
        for table in self.layout.tables:
            seen: set[tuple[object, ...]] = set()
            for row in self.tables[table.name]:
                validate_row(self.layout, table, row)
                key = tuple(row[k] for k in table.primary)
                if key in seen:
                    raise ValueError(f'duplicate record in {table.name}')
                seen.add(key)
            for parent in table.parents:
                keys = self.schema[parent].primary
                owners = {tuple(row[k] for k in keys) for row in self.tables[parent]}
                if any(
                    tuple(row[k] for k in keys) not in owners for row in self.tables[table.name]
                ):
                    raise ValueError(f'missing owner in {table.name}')
            if 'valid_to' in {c.name for c in table.columns}:
                periods: dict[tuple[object, ...], list[tuple[str, str]]] = {}
                for row in self.tables[table.name]:
                    key = tuple(row[k] for k in table.primary if k != 'valid_from')
                    periods.setdefault(key, []).append(
                        (str(row['valid_from']), str(row['valid_to']))
                    )
                for intervals in periods.values():
                    ordered = sorted(intervals)
                    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
                        raise ValueError(f'overlapping SCD intervals in {table.name}')
        for row in self.tables['tb_config_group']:
            if any(
                row[name + '__state'] != 'VALUE'
                for name in ('entity_type', 'priority', 'when', 'then_set')
            ):
                raise ValueError('ConfigGroup requires entity_type, priority, when and then_set')
            target = str(row['entity_type'])
            if target not in self.layout.roots:
                raise ValueError('ConfigGroup target must be a business entity')
            for mapping_table in ('tb_config_group_when', 'tb_config_group_then_set'):
                for member in self.tables[mapping_table]:
                    if member['config_group_id'] != row['config_group_id']:
                        continue
                    field = self.layout.fields.get(str(member['field']))
                    if field is None or field.root != self.layout.roots[target]:
                        raise ValueError('ConfigGroup field is outside its target inheritance tree')
