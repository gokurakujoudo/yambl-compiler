"""Build atomic candidate changes and preserve later edits during rollback."""

from dataclasses import dataclass

from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.model import (
    Change,
    Condition,
    Image,
    Operation,
    Plan,
    Transaction,
    TransactionGroup,
    Value,
)
from yambl_compiler.transactions.writing import literal


@dataclass(frozen=True)
class Claim:
    """Identify an operation's write footprint before publishing any changes.

    :param root: Shared entity identity root.
    :param id: Canonical ID spelling.
    :param field: Logical field, * for deletion, + for insertion.
    :param conditions: Boundable scope conditions.
    """

    root: str
    id: str
    field: str
    conditions: tuple[Condition, ...]


def overlaps(left: Claim, right: Claim) -> bool:
    """Detect overlapping write footprints, including wildcard coordinates.

    :param left: Earlier operation claim.
    :param right: New claim.
    :returns: Whether both operations write the same logical data.
    """
    if (left.root, left.id) != (right.root, right.id):
        return False
    if '*' in (left.field, right.field):
        return True
    if '+' in (left.field, right.field):
        return left.field == right.field
    if left.field != right.field:
        return False
    other = {c.name: c for c in right.conditions}
    for c in left.conditions:
        if c.name not in other:
            continue
        d = other[c.name]
        if c.end is None and d.end is None and c.value != d.value:
            return False
        if (
            c.end is not None
            and d.end is not None
            and (str(c.end) <= str(d.value) or str(d.end) <= str(c.value))
        ):
            return False
    return True


class Execution:
    """Apply one plan to a detached candidate using shared field codecs."""

    def __init__(self, data: Data) -> None:
        """Start with no write claims.

        :param data: Candidate snapshot.
        :returns: None.
        """
        self.data = data
        self.claims: list[Claim] = []

    def claim(self, target: str, id_: Value, field: str, conditions: tuple[Condition, ...]) -> None:
        """Reject overlap with any previously accepted operation.

        :param target: Entity type.
        :param id_: Business ID.
        :param field: Logical field or entity operation marker.
        :param conditions: Scope coordinates.
        :returns: None.
        :raises ValueError: If the Plan contains overlapping writes.
        """
        entity = self.data.catalog.entity(target)
        id_text = str(self.data.catalog.scalar(entity.id.type, id_))
        claim = Claim(entity.root, id_text, field, conditions)
        if any(overlaps(previous, claim) for previous in self.claims):
            raise ValueError(f'overlapping operations for {target}.{field}')
        self.claims.append(claim)

    def field(self, operation: Operation) -> Change:
        """Execute one complete field scope and retain its before/after images.

        :param operation: Field replacement or deletion.
        :returns: Actual scope change.
        """
        entity, field, stored = self.data.catalog.target(operation.target)
        if field is None or stored is None:
            raise ValueError('scope operation requires a field target')
        owner = next((c for c in operation.scope if c.name == entity.id.name), None)
        if owner is None or owner.end is not None:
            raise ValueError('field scope requires owner ID')
        actual = self.data.actual(entity, owner.value)
        actual_field, _ = self.data.catalog.field(actual, field.name)
        self.claim(actual.definition.identity, owner.value, actual_field.identity, operation.scope)
        before = self.data.image(operation.target, operation.scope)
        if operation.kind == 'delete':
            self.data.clear(operation.target, operation.scope)
        elif operation.kind == 'replace':
            self.data.put(
                operation.target,
                operation.scope,
                Image(operation.target, operation.columns, operation.rows),
            )
        else:
            raise ValueError('field scopes require replace or delete')
        return Change(
            operation.target,
            operation.scope,
            (before,),
            (self.data.image(operation.target, operation.scope),),
        )

    def entities(self, operation: Operation) -> tuple[Change, ...]:
        """Execute a batch of ID-based entity mutations.

        :param operation: Entity insertion, field update or deletion.
        :returns: One entity change or individual field changes for each row.
        """
        entity = self.data.catalog.entity(operation.target)
        if entity.id.name not in operation.columns:
            raise ValueError('entity operation requires its ID column')
        if operation.kind == 'delete' and operation.columns != (entity.id.name,):
            raise ValueError('entity deletion accepts only the ID column')
        changes: list[Change] = []
        for values in operation.rows:
            record = dict(zip(operation.columns, values, strict=True))
            id_ = record.pop(entity.id.name)
            scope = (Condition(entity.id.name, id_),)
            if operation.kind == 'insert':
                self.claim(operation.target, id_, '+', scope)
                self.data.insert(entity, id_)
                actual = entity
            else:
                actual = self.data.actual(entity, id_)
            if operation.kind == 'delete':
                self.claim(operation.target, id_, '*', scope)
                entity_before = self.data.entity_images(actual, id_)
                self.data.delete(actual, id_, operation.owned_data)
                changes.append(Change(actual.definition.identity, scope, entity_before, ()))
                continue
            for name, value in record.items():
                field, stored = self.data.catalog.field(actual, name)
                if stored.dimensions or field.category in ('snapshot', 'scd') or stored.members:
                    raise ValueError('coordinate and collection fields require a field scope')
                target = actual.definition.identity + '.' + name
                self.claim(actual.definition.identity, id_, field.identity, scope)
                before = self.data.image(target, scope)
                self.data.put(target, scope, Image(target, ('value',), ((value,),)))
                if operation.kind == 'update':
                    changes.append(
                        Change(target, scope, (before,), (self.data.image(target, scope),))
                    )
            if operation.kind == 'insert':
                changes.append(
                    Change(
                        actual.definition.identity, scope, (), self.data.entity_images(actual, id_)
                    )
                )
            elif operation.kind != 'update':
                raise ValueError('invalid entity operation')
        return tuple(changes)

    def apply(self, plan: Plan) -> tuple[TransactionGroup, ...]:
        """Apply all groups to the candidate, preserving the original boundaries.

        :param plan: Model-bound Plan.
        :returns: Actual changes grouped by Plan group.
        :raises ValueError: On model mismatch or invalid operations.
        """
        if plan.model != self.data.layout.model.model_digest:
            raise ValueError('Plan model digest does not match the repository')
        groups: list[TransactionGroup] = []
        for group in plan.groups:
            changes: list[Change] = []
            for operation in group.operations:
                if operation.scope:
                    changes.append(self.field(operation))
                else:
                    changes.extend(self.entities(operation))
            groups.append(TransactionGroup(group.name, tuple(changes)))
        self.data.validate()
        return tuple(groups)


def comparable(image: Image) -> tuple[tuple[Value, ...], ...]:
    """Canonicalize unordered records and equivalent adjacent SCD fragments.

    :param image: Typed business data image.
    :returns: Sorted semantic records.
    """
    if 'valid_from' not in image.columns:
        return tuple(sorted(image.rows, key=lambda r: tuple(literal(v) for v in r)))
    start, end = image.columns.index('valid_from'), image.columns.index('valid_to')
    groups: dict[tuple[Value, ...], list[tuple[Value, ...]]] = {}
    for row in image.rows:
        key = tuple(v for i, v in enumerate(row) if i not in (start, end))
        groups.setdefault(key, []).append(row)
    merged: list[tuple[Value, ...]] = []
    for rows in groups.values():
        pieces: list[tuple[Value, ...]] = []
        for row in sorted(rows, key=lambda r: str(r[start])):
            if pieces and pieces[-1][end] == row[start]:
                previous = list(pieces[-1])
                previous[end] = row[end]
                pieces[-1] = tuple(previous)
            else:
                pieces.append(row)
        merged.extend(pieces)
    return tuple(sorted(merged, key=lambda r: tuple(literal(v) for v in r)))


def same(left: tuple[Image, ...], right: tuple[Image, ...]) -> bool:
    """Compare full images independently of table row ordering.

    :param left: Current business data.
    :param right: Recorded transaction data.
    :returns: Whether both images represent identical data.
    """
    return len(left) == len(right) and all(
        a.target == b.target and a.columns == b.columns and comparable(a) == comparable(b)
        for a, b in zip(
            sorted(left, key=lambda i: i.target), sorted(right, key=lambda i: i.target), strict=True
        )
    )


def rollback(
    data: Data, transaction: Transaction, selected: tuple[str, ...] | None = None
) -> tuple[TransactionGroup, ...]:
    """Reverse selected actual changes while retaining conflicting later data.

    :param data: Detached candidate snapshot.
    :param transaction: Validated source execution record.
    :param selected: Unique group names, or None for every recorded group.
    :returns: Actual reverse changes and conflict warnings.
    :raises ValueError: On invalid selection or model mismatch.
    """
    if transaction.plan.model != data.layout.model.model_digest:
        raise ValueError('Transaction model digest does not match the repository')
    names = {g.name for g in transaction.groups}
    if selected is not None and (len(set(selected)) != len(selected) or set(selected) - names):
        raise ValueError('unknown or duplicate rollback group')
    result: list[TransactionGroup] = []
    for group in reversed(transaction.groups):
        if selected is not None and group.name not in selected:
            continue
        changes: list[Change] = []
        warnings: list[str] = []
        for change in reversed(group.changes):
            entity, field, _ = data.catalog.target(change.target)
            id_ = next(c.value for c in change.scope if c.name == entity.id.name)
            encoded = data.catalog.scalar(entity.id.type, id_)
            exists = any(r[entity.column] == encoded for r in data.tables[entity.table])
            if exists:
                try:
                    actual = data.actual(entity, id_)
                    if field is not None:
                        data.catalog.field(actual, field.name)
                except ValueError:
                    warnings.append(f'{change.target}: later entity type retained')
                    continue
            if field is None:
                current = data.entity_images(entity, id_) if exists else ()
            else:
                current = (data.image(change.target, change.scope),) if exists else ()
            if not same(current, change.after):
                warnings.append(f'{change.target}: later or missing data retained')
                continue
            if field is None:
                if exists:
                    data.delete(entity, id_, True)
                if change.before:
                    restored = data.catalog.entity(change.before[0].target)
                    data.insert(restored, id_)
                    for image in change.before[1:]:
                        data.put(image.target, change.scope, image)
                after = data.entity_images(entity, id_) if change.before else ()
            else:
                if len(change.before) != 1:
                    raise ValueError('field rollback requires one before image')
                data.put(change.target, change.scope, change.before[0])
                after = (data.image(change.target, change.scope),)
            changes.append(Change(change.target, change.scope, current, after))
        result.append(TransactionGroup(group.name, tuple(changes), tuple(warnings)))
    data.validate()
    return tuple(result)
