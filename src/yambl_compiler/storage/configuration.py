"""Resolve effective per-instance configuration against the pinned repository."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from yambl_compiler.ir.model import Type
from yambl_compiler.storage.backends import Selection
from yambl_compiler.storage.batch import Batch, FieldRequest
from yambl_compiler.storage.contract import decode, loads
from yambl_compiler.storage.records import STATE_CODES, mapped_field
from yambl_compiler.storage.results import (
    EntityRef,
    NonNormalResult,
    ReadResult,
    Status,
    Success,
)


@dataclass(frozen=True)
class Entry:
    """Retain one typed configuration mapping entry.

    :param field: Logical field identity.
    :param type: Embedded value contract.
    :param value: Encoded scalar payload.
    :param decoded: Value checked against the embedded contract.
    """

    field: str
    type: Type
    value: object
    decoded: object


@dataclass(frozen=True)
class Group:
    """Describe one validated conditional configuration group.

    :param id: Group reference ID.
    :param target: Entity type including its subclasses.
    :param priority: Arbitrary-precision priority, larger wins.
    :param conditions: Stored static equalities, all required.
    :param assignments: Per-field configuration values.
    """

    id: str
    target: str
    priority: int
    conditions: tuple[Entry, ...]
    assignments: tuple[Entry, ...]


class Configuration:
    """Cache effective configuration by actual entity and logical config field."""

    def __init__(self, batch: Batch) -> None:
        """Retain a shared batch reader and initially unloaded groups.

        :param batch: Pinned session's batch facade.
        :returns: None.
        """
        self.batch = batch
        self.groups: tuple[Group, ...] | None = None
        self.cache: dict[tuple[EntityRef[object], str], ReadResult[object]] = {}

    def value(self, type_: Type, payload: object) -> object:
        """Decode a typed group scalar or immutable reference collection.

        :param type_: Required field contract.
        :param payload: JSON scalar or array of encoded reference IDs.
        :returns: Checked scalar or immutable unordered references.
        :raises ValueError: On invalid collection shape or duplicate references.
        """
        if type_.name == "collection":
            if not isinstance(payload, list):
                raise ValueError("ConfigGroup collection requires an array of reference IDs")
            values = frozenset(
                self.batch.reader.decode(type_.arguments[0], item)
                for item in cast(list[object], payload)
            )
            if len(values) != len(cast(list[object], payload)):
                raise ValueError("duplicate ConfigGroup collection reference")
            return values
        return self.batch.reader.decode(type_, payload)

    def load(self) -> tuple[Group, ...]:
        """Load all groups once without interpreting priorities as SQLite integers.

        :returns: Validated group contracts in deterministic ID order.
        :raises ValueError: On invalid group metadata or mapping values.
        """
        if self.groups is not None:
            return self.groups
        reader = self.batch.reader
        reader.ready()
        headers = reader.backend.rows_many("tb_config_group", [Selection(())])[0]
        mappings: dict[str, dict[str, list[Entry]]] = {"when": {}, "then_set": {}}
        known = {str(row["config_group_id"]) for row in headers}
        logical = {f.identity: f for f in reader.layout.model.fields}
        for role in mappings:
            for row in reader.backend.rows_many("tb_config_group_" + role, [Selection(())])[0]:
                group_id, identity = str(row["config_group_id"]), str(row["field"])
                if group_id not in known or identity not in logical:
                    raise ValueError("ConfigGroup mapping references an unknown group or field")
                envelope = loads(str(row["value"]))
                if not isinstance(envelope, dict) or set(envelope) != {"type", "value"}:
                    raise ValueError("invalid ConfigGroup typed value envelope")
                type_ = cast(Type, decode(envelope["type"], Type))
                if type_ != logical[identity].type:
                    raise ValueError(
                        "ConfigGroup mapping value type disagrees with the field contract"
                    )
                field = reader.implementations[logical[identity].implementations[0]]
                if field.category != ("static" if role == "when" else "config"):
                    raise ValueError(
                        "ConfigGroup requires stored static conditions and config assignments"
                    )
                decoded = self.value(type_, envelope["value"])
                entries = mappings[role].setdefault(group_id, [])
                if any(e.field == identity for e in entries):
                    raise ValueError("duplicate ConfigGroup mapping field")
                entries.append(Entry(identity, type_, envelope["value"], decoded))
        groups: list[Group] = []
        for row in headers:
            group_id = str(row["config_group_id"])
            if any(
                row[name + "__state"] != STATE_CODES['VALUE']
                for name in ("priority", "target_entity_type", "when", "then_set")
            ):
                raise ValueError(
                    f"ConfigGroup {group_id} requires priority, target_entity_type, when and then_set"
                )
            raw = row["priority"]
            if not isinstance(raw, str):
                raise ValueError("ConfigGroup priority requires an exact integer string")
            priority = int(raw)
            if str(priority) != raw:
                raise ValueError("ConfigGroup priority is not a canonical integer")
            target = str(row["target_entity_type"])
            if (
                target not in reader.layout.entities
                or reader.layout.entities[target].source.origin != 'model'
            ):
                raise ValueError("ConfigGroup has an unknown target entity type")
            conditions = tuple(sorted(mappings["when"].get(group_id, []), key=lambda e: e.field))
            assignments = tuple(
                sorted(mappings["then_set"].get(group_id, []), key=lambda e: e.field)
            )
            for entry in (*conditions, *assignments):
                mapped = mapped_field(reader.layout, entry.field)
                if mapped is None or reader.layout.roots[target] != mapped[1]:
                    raise ValueError(
                        "ConfigGroup mapping field belongs to another inheritance root"
                    )
            groups.append(Group(group_id, target, priority, conditions, assignments))
        if len({g.id for g in groups}) != len(groups):
            raise ValueError("duplicate ConfigGroup identity")
        self.groups = tuple(sorted(groups, key=lambda g: g.id))
        return self.groups

    def read(self, requests: Sequence[FieldRequest]) -> tuple[ReadResult[object], ...]:
        """Resolve group overrides, then instance values and actual-type defaults.

        :param requests: Config field requests, retaining contribution order.
        :returns: Effective values with source groups, or non-normal results.
        """
        pending = list(
            dict.fromkeys(
                (r.owner, r.identity) for r in requests if (r.owner, r.identity) not in self.cache
            )
        )
        if not pending:
            return tuple(self.cache[(r.owner, r.identity)] for r in requests)
        groups = self.load()
        needed = list(
            dict.fromkeys(
                FieldRequest(owner, condition.field)
                for owner, _ in pending
                for group in groups
                if self.batch.is_type(owner.target, group.target)
                for condition in group.conditions
            )
        )
        observed = dict(zip(needed, self.batch.read(needed), strict=True))
        computed: dict[tuple[EntityRef[object], str], ReadResult[object]] = {}
        fallbacks: list[FieldRequest] = []
        for owner, identity in pending:
            field = self.batch.implementation(owner.target, identity)
            if field is None:
                computed[(owner, identity)] = NonNormalResult(Status.NOT_DEFINED)
                continue
            candidates: list[tuple[Group, Entry]] = []
            failed: NonNormalResult | None = None
            for group in groups:
                if not self.batch.is_type(owner.target, group.target):
                    continue
                assignment = next((a for a in group.assignments if a.field == identity), None)
                if assignment is None:
                    continue
                matches = True
                for condition in group.conditions:
                    actual = observed[FieldRequest(owner, condition.field)]
                    if isinstance(actual, NonNormalResult):
                        if actual.status == Status.ERROR:
                            failed = actual
                        matches = False
                    else:
                        expected = condition.decoded
                        matches &= actual.value == expected
                if matches:
                    candidates.append((group, assignment))
            if failed is not None:
                computed[(owner, identity)] = failed
                continue
            if not candidates:
                fallbacks.append(FieldRequest(owner, identity))
                continue
            highest = max(g.priority for g, _ in candidates)
            selected = [(g, e) for g, e in candidates if g.priority == highest]
            try:
                values = [self.value(field.type, entry.value) for _, entry in selected]
                if any(v != values[0] for v in values[1:]):
                    computed[(owner, identity)] = NonNormalResult(
                        Status.ERROR, "conflicting ConfigGroups at the highest priority"
                    )
                else:
                    computed[(owner, identity)] = Success(
                        values[0], "config_group", tuple(g.id for g, _ in selected)
                    )
            except (ValueError, TypeError, KeyError) as error:
                computed[(owner, identity)] = NonNormalResult(Status.ERROR, str(error))
        for request, result in zip(fallbacks, self.batch.read(fallbacks), strict=True):
            computed[(request.owner, request.identity)] = result
        for key, result in computed.items():
            if not isinstance(result, NonNormalResult) or result.status != Status.ERROR:
                self.cache[key] = result
        return tuple(
            computed.get(
                (r.owner, r.identity),
                self.cache.get((r.owner, r.identity), NonNormalResult(Status.ERROR)),
            )
            for r in requests
        )
