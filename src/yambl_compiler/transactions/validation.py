"""Validate Plan targets and literals using only the generated model contract."""

from datetime import date
from typing import cast

from yambl_compiler.ir.model import Field
from yambl_compiler.languages.shared.model import Symbol, Value
from yambl_compiler.languages.yaplan.model import Operation, Plan
from yambl_compiler.storage.records import Layout, StoredField
from yambl_compiler.transactions.binding import Entity, object_items
from yambl_compiler.transactions.editing import STATES, Data
from yambl_compiler.transactions.execution import Execution


class PlanValidator:
    """Check model-bound Plan contents without business data or storage writes."""

    def __init__(self, layout: Layout) -> None:
        """Retain schema helpers with empty, unused business tables.

        :param layout: Bundled, validated storage contract.
        :returns: None.
        """
        self.data = Data(layout, {})
        self.catalog = self.data.catalog
        self.codes: dict[str, set[str]] = {}
        self.catalog.code_members = self.codes.get

    def track_dictionary(self, operation: Operation) -> None:
        """Track a complete local dictionary replacement and protect its original entries.

        :param operation: Already type-checked Plan operation.
        :returns: None after recording a known resulting code set.
        :raises ValueError: If the replacement changes model-provided entries.
        """
        if operation.target != 'builtins.Decodes.entries':
            return
        identity = cast(str, next(c.value for c in operation.scope if c.name == 'decodes_id'))
        definition = self.catalog.definitions.get(identity)
        if definition is None or definition.kind != 'decodes':
            return
        value = (
            operation.rows[0][operation.columns.index('value')]
            if operation.kind == 'replace' and operation.rows
            else ()
        )
        entries = [object_items(item) for item in value] if isinstance(value, tuple) else []
        provided = {(item['code'], item['decode'], item['ordering']) for item in entries}
        original = {(entry.code, entry.decode, int(entry.ordering)) for entry in definition.entries}
        if not original <= provided:
            raise ValueError(f'model dictionary entries are immutable: {identity}')
        self.codes[identity] = {str(item['code']) for item in entries}

    def track_hierarchy(self, operation: Operation) -> None:
        """Protect model-declared Hierarchy entries during Plan validation.

        :param operation: Already type-checked Plan operation.
        :returns: None when model entries are preserved.
        :raises ValueError: If the replacement changes a declared entry.
        """
        if operation.target != 'builtins.Hierarchy.hierarchy_entries':
            return
        identity = cast(str, next(c.value for c in operation.scope if c.name == 'hierarchy_id'))
        definition = self.catalog.definitions.get(identity)
        if definition is None or definition.kind != 'hierarchy':
            return
        value = (
            operation.rows[0][operation.columns.index('value')]
            if operation.kind == 'replace' and operation.rows
            else ()
        )
        entries = [object_items(item) for item in value] if isinstance(value, tuple) else []
        provided = {(item['ordering'], item['entry_value']) for item in entries}
        original = {(int(entry.ordering), entry.value) for entry in definition.hierarchy_entries}
        if not original <= provided:
            raise ValueError(f'model hierarchy entries are immutable: {identity}')

    def value(self, entity: Entity, field: Field, stored: StoredField, value: Value) -> None:
        """Check a field literal using the same encoders as apply.

        :param entity: Declared owner type.
        :param field: Effective field declaration.
        :param stored: Field storage contract.
        :param value: Literal to check.
        :returns: None.
        :raises ValueError: On incompatible values or duplicate members.
        """
        if isinstance(value, Symbol) and value.name in STATES:
            return
        if stored.members:
            keys = self.data.schema[stored.members].primary
            # Owner columns are irrelevant to member literal encoding.
            row: dict[str, object] = {key: None for key in keys}
            members = self.data.encode_members(entity, field, stored, row, value)
            identities = [tuple(member[key] for key in keys) for member in members]
            if len(set(identities)) != len(identities):
                raise ValueError('duplicate collection member')
        else:
            self.catalog.scalar(field.type, value)

    def operation(self, operation: Operation) -> None:
        """Check one entity batch or field scope against the model.

        :param operation: Parsed operation.
        :returns: None.
        :raises ValueError: On invalid names, coordinates, types or operation shapes.
        """
        entity, field, stored = self.catalog.target(operation.target)
        if field is None or stored is None:
            if operation.scope or operation.kind == 'replace':
                raise ValueError('scope operation requires a field target')
            if entity.id.name not in operation.columns:
                raise ValueError('entity operation requires its ID column')
            for index, name in enumerate(operation.columns):
                if name == entity.id.name:
                    for row in operation.rows:
                        self.catalog.scalar(entity.id.type, row[index])
                    continue
                current, binding = self.catalog.field(entity, name)
                if (
                    operation.kind == 'delete'
                    or binding.dimensions
                    or current.category in ('snapshot', 'scd')
                    or (binding.members and operation.kind != 'insert')
                ):
                    raise ValueError('field requires a scope or is not writable')
                for row in operation.rows:
                    self.value(entity, current, binding, row[index])
            return
        if not operation.scope or operation.kind not in ('replace', 'delete'):
            raise ValueError('field operations require replace or delete with scope')
        filters, interval = self.data.selection(entity, field, stored, operation.scope)
        if operation.kind == 'delete':
            return
        constants = {c.name: c.value for c in operation.scope if c.end is None}
        coordinates = self.data.coordinates(entity, field, stored)
        expected = set(coordinates) | {'value'}
        if (
            set(operation.columns) - expected
            or expected - set(operation.columns) - constants.keys()
        ):
            raise ValueError('replacement columns must provide all coordinates and value')
        types = {entity.id.name: entity.id.type, **{d.key: d.type for d in stored.dimensions}}
        for row in operation.rows:
            record = {**constants, **dict(zip(operation.columns, row, strict=True))}
            physical: dict[str, object] = dict(stored.key_values)
            for name, column in coordinates.items():
                value = record[name]
                if name in types:
                    physical[column] = self.catalog.scalar(types[name], value)
                elif not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError('expected ISO temporal coordinate')
                else:
                    physical[column] = value
            if not self.data.matches(physical, filters, interval, field.category):
                raise ValueError('replacement record is outside scope')
            if (
                field.category == 'scd'
                and interval is not None
                and not (
                    interval[0]
                    <= str(physical['valid_from'])
                    < str(physical['valid_to'])
                    <= interval[1]
                )
            ):
                raise ValueError('replacement interval crosses scope boundary')
            self.value(entity, field, stored, record['value'])

    def validate(self, plan: Plan) -> None:
        """Check the digest and every operation, including zero-row headers.

        :param plan: Parsed Plan.
        :returns: None.
        :raises ValueError: On model mismatch or invalid operations.
        """
        if plan.model is not None and plan.model != self.data.layout.model.model_digest:
            raise ValueError('Plan model digest does not match the repository')
        self.codes.clear()
        Execution(self.data).validate_claims(plan)
        for group in plan.groups:
            for operation in group.operations:
                self.operation(operation)
                self.track_dictionary(operation)
                self.track_hierarchy(operation)
