"""Edit and physically validate affected candidate rows on a pinned snapshot."""

from collections.abc import Callable, Mapping

from yambl_compiler.ir.model import Field
from yambl_compiler.storage.backends import Backend, Row, validate_row
from yambl_compiler.storage.records import Layout, StoredField, table_name
from yambl_compiler.storage.results import NonNormalResult, ReadResult, Status
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.incremental_validation import validate_affected_cycles
from yambl_compiler.transactions.overlay import Overlay


class IncrementalData(Data):
    """Edit selected rows while retaining their original images for publication.

    Existing, unchanged rows are assumed to have passed the previous validation.
    Full data checking remains the responsibility of the explicit reader check.
    """

    def __init__(self, layout: Layout, backend: Backend) -> None:
        """Bind the candidate to a caller-owned pinned source backend.

        :param layout: Generated physical storage layout.
        :param backend: Pinned read snapshot, owned by the caller.
        :returns: None.
        """
        super().__init__(layout, {})
        self.overlay = Overlay(layout, backend)

    def select(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Select candidate rows by physical equality coordinates.

        :param table: Physical table name.
        :param keys: Equality conditions.
        :returns: Mutable candidate-owned matching rows.
        """
        return self.overlay.select(table, keys)

    def add(self, table: str, row: Row) -> None:
        """Add one detached row to the candidate.

        :param table: Physical table name.
        :param row: Row to add.
        :returns: None.
        """
        self.overlay.add(table, row)

    def remove(self, table: str, row: Row) -> None:
        """Remove a previously selected candidate row.

        :param table: Physical table name.
        :param row: Candidate-owned row.
        :returns: None.
        """
        self.overlay.remove(table, row)

    def validate(self) -> None:
        """Validate affected physical records, ownership, periods and configuration.

        Same-field reference cycles require the separate candidate graph check.

        :returns: None.
        :raises ValueError: If affected data violates the physical contract.
        """
        changes = self.overlay.changes()
        affected_groups: set[object] = set()
        for name, records in changes.items():
            table = self.schema[name]
            for change in records:
                if name in ('tb_config_group', 'tb_config_group_when', 'tb_config_group_then_set'):
                    for row in (change.before, change.after):
                        if row is not None:
                            affected_groups.add(row['config_group_id'])
                row = change.after
                if row is None:
                    continue
                validate_row(self.layout, table, row)
                for parent in table.parents:
                    parent_keys = self.schema[parent].primary
                    if not self.select(parent, {key: row[key] for key in parent_keys}):
                        raise ValueError(f'missing owner in {name}')
                if 'valid_to' in {column.name for column in table.columns}:
                    filters = {key: row[key] for key in table.primary if key != 'valid_from'}
                    intervals = sorted(
                        (str(other['valid_from']), str(other['valid_to']))
                        for other in self.select(name, filters)
                    )
                    if any(a[1] > b[0] for a, b in zip(intervals, intervals[1:])):
                        raise ValueError(f'overlapping SCD intervals in {name}')
            for change in records:
                if change.before is None or change.after is not None:
                    continue
                for child in self.layout.tables:
                    if name in child.parents:
                        filters = {key: change.before[key] for key in table.primary}
                        if self.select(child.name, filters):
                            raise ValueError(f'missing owner in {child.name}')
                for stored in self.layout.fields.values():
                    if (
                        stored.root in self.layout.entities
                        and table_name(stored.root) == name
                        and stored.table != name
                    ):
                        filters = {'id': change.before['id'], **dict(stored.key_values)}
                        if self.select(stored.table, filters):
                            raise ValueError(f'missing owner in {stored.table}')
        for group_id in affected_groups:
            for row in self.select('tb_config_group', {'config_group_id': group_id}):
                if any(
                    row[name + '__state'] != 'VALUE'
                    for name in ('entity_type', 'priority', 'when', 'then_set')
                ):
                    raise ValueError(
                        'ConfigGroup requires entity_type, priority, when and then_set'
                    )
                target = str(row['entity_type'])
                if target not in self.layout.roots:
                    raise ValueError('ConfigGroup target must be a business entity')
                for mapping_table in ('tb_config_group_when', 'tb_config_group_then_set'):
                    for member in self.select(mapping_table, {'config_group_id': group_id}):
                        field = self.layout.fields.get(str(member['field']))
                        if field is None or field.root != self.layout.roots[target]:
                            raise ValueError(
                                'ConfigGroup field is outside its target inheritance tree'
                            )

    def validate_business(
        self,
        field: Callable[[str, str], Field | None],
        read: Callable[[StoredField, Field, Row], ReadResult[object]],
    ) -> None:
        """Check affected field values and cycles against candidate-backed reads.

        :param field: Resolve an actual entity field implementation.
        :param read: Decode candidate field values, including collection members.
        :returns: None.
        :raises ValueError: If changed fields or reachable references are invalid.
        """
        changes = self.overlay.changes()
        changed: dict[str, list[Row]] = {}
        for name, records in changes.items():
            changed[name] = [
                row
                for change in records
                for row in (change.before, change.after)
                if row is not None
            ]
            for change in records:
                row = change.after
                if row is None:
                    continue
                for identity, stored in self.layout.fields.items():
                    if (
                        stored.table != name
                        or stored.root not in self.layout.entities
                        or any(row[key] != value for key, value in stored.key_values)
                    ):
                        continue
                    owners = self.select(table_name(stored.root), {'id': row['id']})
                    if len(owners) != 1:
                        raise ValueError(f'missing or invalid owner entity: {identity}')
                    actual = str(owners[0]['entity_type'])
                    if (
                        actual not in self.layout.entities
                        or self.layout.roots[actual] != stored.root
                    ):
                        raise ValueError(f'missing or invalid owner entity: {identity}')
                    implementation = field(actual, identity)
                    if implementation is not None:
                        value = read(stored, implementation, row)
                        if isinstance(value, NonNormalResult) and value.status == Status.ERROR:
                            raise ValueError(f'invalid field {identity}: {value.message}')
        report = validate_affected_cycles(self.layout, self.select, field, read, changed)
        if not report.valid:
            raise ValueError(f'candidate data validation failed: {report}')
