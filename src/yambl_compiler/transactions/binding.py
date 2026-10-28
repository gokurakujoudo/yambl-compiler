"""Bind business transaction values and targets to a generated model contract."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import cast

from yambl_compiler.ir.model import Definition, Field, Type
from yambl_compiler.languages.shared.model import ObjectValue, Symbol, Value
from yambl_compiler.storage.records import Layout, StoredField, table_name


@dataclass(frozen=True)
class Entity:
    """Locate an entity's identity and inherited storage root.

    :param definition: Actual entity declaration.
    :param root: Shared identity root.
    :param id: Declared business ID field.
    :param table: Physical identity table.
    :param column: Physical ID column.
    """

    definition: Definition
    root: str
    id: Field
    table: str
    column: str


class Catalog:
    """Resolve transaction names using the compiled model's effective fields."""

    def __init__(self, layout: Layout) -> None:
        """Index declarations without loading model source files.

        :param layout: Validated generated storage contract.
        :returns: None.
        """
        self.layout = layout
        self.definitions = {d.identity: d for d in layout.model.definitions}
        self.implementations = {
            f.implementation: f for d in layout.model.definitions for f in d.fields
        }
        self.code_members: Callable[[str], set[str] | None] | None = None

    def entity(self, name: str) -> Entity:
        """Resolve a supported business or built-in entity.

        :param name: Fully qualified entity name.
        :returns: Entity identity binding.
        :raises ValueError: On an unknown or unsupported target.
        """
        definition = self.definitions.get(name)
        if definition is None or definition.kind != 'entity':
            raise ValueError(f'unknown entity: {name}')
        if definition.source.origin == 'model' and name in self.layout.roots:
            root = self.layout.roots[name]
            return Entity(definition, root, self.layout.ids[root], table_name(root), 'id')
        if name not in ('builtins.Decodes', 'builtins.Hierarchy', 'builtins.ConfigGroup'):
            raise ValueError(f'entity has no storage: {name}')
        id_ = next(f for f in definition.fields if f.category == 'id')
        return Entity(definition, name, id_, table_name(name), id_.name)

    def fields(self, entity: Entity) -> dict[str, Field]:
        """Select actual field implementations, including inherited declarations.

        :param entity: Actual entity binding.
        :returns: Effective fields by business name.
        """
        fields = (
            tuple(self.implementations[e.implementation] for e in entity.definition.effective)
            if entity.definition.source.origin == 'model'
            else entity.definition.fields
        )
        return {f.name: f for f in fields}

    def check_writable_identity(self, entity: Entity, id_: str, *, deleting: bool = False) -> None:
        """Reject writes to model-derived dictionary instances.

        :param entity: Target entity binding.
        :param id_: Canonical entity ID.
        :param deleting: Whether the whole entity is being deleted.
        :returns: None when the identity is writable.
        :raises ValueError: If the dictionary is derived from the model.
        """
        if entity.definition.identity == 'builtins.Decodes':
            definition = self.definitions.get(id_)
            if definition is not None and definition.kind == 'decodes':
                if definition.derived:
                    raise ValueError(f'derived dictionary is read-only: {id_}')
                if any(call.target == 'builtins.flags' for call in definition.decorators):
                    raise ValueError(f'flags dictionary is read-only: {id_}')
                if deleting:
                    raise ValueError(f'model dictionary cannot be deleted: {id_}')

    def field(self, entity: Entity, name: str) -> tuple[Field, StoredField]:
        """Resolve an effective writable field.

        :param entity: Actual entity binding.
        :param name: Unqualified business field name.
        :returns: Effective declaration and storage mapping.
        :raises ValueError: On an absent, ID or computed field.
        """
        field = self.fields(entity).get(name)
        if field is None or field.builtin or field.category in ('id', 'computed'):
            raise ValueError(f'field is not writable: {entity.definition.identity}.{name}')
        stored = self.layout.fields.get(field.identity)
        if stored is None:
            raise ValueError(f'field has no storage: {field.identity}')
        return field, stored

    def target(self, name: str) -> tuple[Entity, Field | None, StoredField | None]:
        """Resolve an entity or qualified field operation target.

        :param name: Qualified target.
        :returns: Entity and optional field bindings.
        :raises ValueError: On an unresolved or unwritable target.
        """
        if name in self.definitions:
            return self.entity(name), None, None
        owner, _, field_name = name.rpartition('.')
        entity = self.entity(owner)
        field, stored = self.field(entity, field_name)
        return entity, field, stored

    def scalar(self, type_: Type, value: Value, *, check_code: bool = True) -> str | int:
        """Validate a data literal and encode a physical scalar payload.

        :param type_: Resolved declared type.
        :param value: Parsed literal or model symbol.
        :param check_code: Whether to validate local dictionary membership.
        :returns: Canonical storage text or boolean integer.
        :raises ValueError: On incompatible literals or declared constraints.
        """
        encoded: str | int
        if type_.kind == 'reference':
            encoded = self.scalar(self.entity(type_.target).id.type, value)
        elif type_.name == 'bool':
            if type(value) is not bool:
                raise ValueError('expected bool literal')
            encoded = int(value)
        elif type_.name in ('integer', 'snowflake_id'):
            if type(value) is not int:
                raise ValueError('expected integer literal')
            encoded = str(value)
        elif type_.name == 'decimal':
            if type(value) not in (int, Decimal):
                raise ValueError('expected decimal literal')
            number = Decimal(cast(int | Decimal, value))
            if not number.is_finite():
                raise ValueError('expected finite decimal')
            text = format(number, 'f')
            encoded = text.rstrip('0').rstrip('.') if '.' in text else text
            if number == 0:
                encoded = '0'
            params = {p.name: int(p.value) for p in type_.parameters}
            if params:
                _, digits, exponent = Decimal(encoded).as_tuple()
                scale = max(0, -cast(int, exponent))
                whole = max(0, len(digits) + cast(int, exponent))
                if scale > params['scale'] or whole > params['precision'] - params['scale']:
                    raise ValueError('decimal exceeds declared precision or scale')
        elif type_.name == 'date':
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError('expected ISO date string')
            encoded = value
        elif type_.name == 'code':
            dictionary = type_.arguments[0].target
            if isinstance(value, Symbol):
                owner, _, code = value.name.rpartition('.')
                if owner != dictionary:
                    raise ValueError(f'expected code from {dictionary}')
                value = code
            if not isinstance(value, str):
                raise ValueError('expected code name or string')
            definition = self.definitions.get(dictionary)
            if definition is not None and check_code:
                fixed = definition.derived or any(
                    call.target == 'builtins.flags' for call in definition.decorators
                )
                members = (
                    self.code_members(dictionary)
                    if self.code_members is not None and not fixed
                    else {entry.code for entry in definition.entries}
                )
                if members is not None and value not in members:
                    raise ValueError(f'unknown code for {dictionary}: {value}')
            encoded = value
        elif type_.name == 'type':
            if not isinstance(value, Symbol):
                raise ValueError('expected entity type name')
            entity = self.entity(value.name)
            bound = type_.arguments[0].target
            ancestor = entity.definition
            while ancestor.identity != bound and ancestor.parent in self.definitions:
                ancestor = self.definitions[ancestor.parent]
            if bound not in ('Entity', 'builtins.Entity') and ancestor.identity != bound:
                raise ValueError(f'entity type must derive from {bound}')
            encoded = value.name
        elif type_.name == 'string' and isinstance(value, str):
            encoded = value
        else:
            raise ValueError(f'unsupported literal for {type_.name}')
        if type_.exact is not None:
            compared = str(bool(encoded)).lower() if type_.name == 'bool' else str(encoded)
            if compared != type_.exact.text:
                raise ValueError('value violates exact constraint')
        return encoded

    def literal(self, type_: Type, payload: object) -> Value:
        """Decode a scalar to a portable business literal.

        :param type_: Resolved declared type.
        :param payload: Stored scalar.
        :returns: Checked data literal.
        :raises ValueError: On malformed stored data.
        """
        if type_.kind == 'reference':
            return self.literal(self.entity(type_.target).id.type, payload)
        if type_.name == 'bool':
            if type(payload) is not int or payload not in (0, 1):
                raise ValueError('invalid boolean payload')
            value: Value = bool(payload)
        else:
            if not isinstance(payload, str):
                raise ValueError('expected text payload')
            if type_.name in ('integer', 'snowflake_id'):
                value = int(payload)
                if str(value) != payload:
                    raise ValueError('noncanonical integer payload')
            elif type_.name == 'decimal':
                value = Decimal(payload)
            elif type_.name == 'type':
                value = Symbol(payload)
            else:
                value = payload
        self.scalar(type_, value, check_code=False)
        return value


def object_items(value: Value) -> dict[str, Value]:
    """Read an object with unique string or symbolic keys.

    :param value: Structured object literal.
    :returns: Named members.
    :raises ValueError: On malformed or duplicate keys.
    """
    if not isinstance(value, ObjectValue):
        raise ValueError('expected object literal')
    result: dict[str, Value] = {}
    for key, item in value.items:
        if not isinstance(key, (str, Symbol)):
            raise ValueError('object keys must be names or strings')
        name = key.name if isinstance(key, Symbol) else key
        if name in result:
            raise ValueError(f'duplicate object key: {name}')
        result[name] = item
    return result
