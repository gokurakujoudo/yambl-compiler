"""Match typed keys against an immutable, prepared in-memory index."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from yambl_compiler.storage.results import NonNormalResult, Status, Success
from yambl_compiler.storage.scalars import ScalarRule


@dataclass(frozen=True, slots=True)
class AmbiguousMatch[IdT]:
    """Return the distinct candidate IDs in native order.

    :param candidates: Sorted candidate IDs for one key.
    """

    candidates: tuple[IdT, ...]


type LookupResult[IdT] = Success[IdT] | AmbiguousMatch[IdT] | NonNormalResult


def invalid_batch(value: object) -> bool:
    """Check the public batch container at the runtime boundary.

    :param value: Caller-supplied batch.
    :returns: Whether the value is not a sequence of records.
    """
    return not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray))


@dataclass(frozen=True, slots=True)
class LookupStats:
    """Count candidates and keys processed during preparation.

    :param scanned_entities: Number of candidate entities scanned.
    :param indexed_entities: Number of entities that formed a complete key.
    :param skipped_entities: Number of entities without a complete key.
    :param distinct_keys: Number of indexed keys.
    :param ambiguous_keys: Number of keys with multiple candidate IDs.
    """

    scanned_entities: int
    indexed_entities: int
    skipped_entities: int
    distinct_keys: int
    ambiguous_keys: int


@dataclass(frozen=True, slots=True)
class KeyField:
    """Bind one generated record field to a detached scalar rule.

    :param name: Generated record field name.
    :param rule: Validated type and dictionary contract.
    """

    name: str
    rule: ScalarRule


class Matcher[KeyT, IdT: str | int]:
    """Match records using a detached validator and precomputed results."""

    def __init__(
        self,
        key_type: type[KeyT],
        fields: Sequence[KeyField],
        rows: Sequence[tuple[tuple[object, ...], IdT]],
        skipped: int,
    ) -> None:
        """Build immutable results from successfully resolved candidate keys.

        :param key_type: Generated key record class.
        :param fields: Detached validation rules in key declaration order.
        :param rows: One composite key and ID per successfully indexed candidate.
        :param skipped: Number of candidates without a complete key.
        :returns: None; publishes a detached index and immutable statistics.
        """
        groups: dict[tuple[object, ...], set[IdT]] = {}
        for key, identity in rows:
            groups.setdefault(key, set()).add(identity)
        index: dict[tuple[object, ...], LookupResult[IdT]] = {}
        ambiguous = 0
        for key, identities in groups.items():
            candidates = tuple(sorted(identities))
            if len(candidates) == 1:
                index[key] = Success(candidates[0])
            else:
                ambiguous += 1
                index[key] = AmbiguousMatch(candidates)
        self._key_type = key_type
        self._fields = tuple(fields)
        self._index: Mapping[tuple[object, ...], LookupResult[IdT]] = MappingProxyType(index)
        self._not_found = NonNormalResult(Status.NO_ENTRY)
        self.stats = LookupStats(len(rows) + skipped, len(rows), skipped, len(index), ambiguous)

    def match_many(
        self, keys: Sequence[KeyT]
    ) -> Success[tuple[LookupResult[IdT], ...]] | NonNormalResult:
        """Match an ordered batch without reading the source repository.

        :param keys: Generated key records; repeated inputs retain their positions.
        :returns: Ordered per-key outcomes, or ERROR for an invalid batch container.
        """
        if invalid_batch(keys):
            return NonNormalResult(Status.ERROR, 'lookup keys must be a sequence')
        results: list[LookupResult[IdT]] = []
        for position, key in enumerate(keys):
            if type(key) is not self._key_type:
                results.append(
                    NonNormalResult(Status.ERROR, f'input {position}: invalid key record')
                )
                continue
            values: list[object] = []
            for field in self._fields:
                try:
                    values.append(field.rule.check(getattr(key, field.name)))
                except (TypeError, ValueError) as error:
                    results.append(
                        NonNormalResult(
                            Status.ERROR, f'input {position}, key {field.name}: {error}'
                        )
                    )
                    break
            else:
                results.append(self._index.get(tuple(values), self._not_found))
        return Success(tuple(results))
