"""Represent resolved types and explicitly supported compatibility rules."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast

from lark import Token, Tree

from yambl_compiler.languages.yambl.computation.records import Value, ValueField
from yambl_compiler.languages.yambl.semantics.symbols import EntityKey, Unit, World, qualified
from yambl_compiler.languages.yambl.syntax.parser import Diagnostic, children, tokens

type Kind = Literal['value', 'string', 'reference', 'collection', 'unknown']
type FieldTypes = dict[str, tuple[Unit, Tree[Token]]]
MODES = {'attr', 'static', 'config', 'snapshot', 'scd', "computed"}


@dataclass(frozen=True)
class FiniteValue:
    """Represent a typed compile-time value with stable equality.

    :param kind: Scalar kind or structured value marker.
    :param target: Dictionary, reference, or structured type identity.
    :param value: Immutable scalar or named structured fields.
    """

    kind: str
    target: EntityKey | None
    value: (
        str | int | bool | float | Decimal | date | datetime | tuple[tuple[str, 'FiniteValue'], ...]
    )


type FiniteValueKind = Literal[
    'string',
    'integer',
    'decimal',
    'bool',
    'reference',
    'type',
    'dictionary',
    'code',
    'flags',
    'float',
    'date',
    'datetime',
    'snowflake_id',
    'structured',
]


def portable_finite_value(member: FiniteValue) -> Value:
    """Encode a typed compile-time member without losing its identity.

    :param member: Normalized semantic member.
    :returns: Portable typed value, including structured fields.
    """
    payload = member.value
    if member.kind == 'structured':
        assert isinstance(payload, tuple)
        return Value(
            'structured',
            '',
            qualified(member.target) if member.target else '',
            fields=tuple(ValueField(name, portable_finite_value(value)) for name, value in payload),
        )
    if isinstance(payload, bool):
        text = str(payload).lower()
    elif isinstance(payload, datetime):
        from yambl_compiler.storage.conversions import convert

        text = cast(str, convert('format_datetime', (payload,)))
    elif isinstance(payload, date):
        text = payload.isoformat()
    elif isinstance(payload, Decimal):
        sign, digits, exponent = payload.as_tuple()
        assert isinstance(exponent, int)
        remaining = list(digits)
        while len(remaining) > 1 and remaining[-1] == 0:
            remaining.pop()
            exponent += 1
        text = '0' if payload == 0 else str(Decimal((sign, tuple(remaining), exponent)))
    elif isinstance(payload, float):
        text = '0.0' if payload == 0 else repr(payload)
    else:
        text = str(payload)
    return Value(
        cast(FiniteValueKind, member.kind), text, qualified(member.target) if member.target else ''
    )


@dataclass(frozen=True)
class Resolved:
    """Describe a type after transparent aliases have been expanded.

    :param name: Primitive or resolved declaration name.
    :param kind: Storage category.
    :param args: Generic constraints.
    :param key: Resolved declaration identity.
    :param params: Normalized descriptive parameters.
    :param exact: Required constant, if constrained to one value.
    :param dependency: Instance-dependent selection or alias path.
    :param members: Non-collection types available through an attrs key.
    :param exact_key: Structured declaration identity of a type or dictionary constant.
    :param exact_value: Typed value of a calculated singleton constraint.
    :param allowed_values: Unordered finite value constraint, or empty when unconstrained.
    """

    name: str
    kind: Kind = 'value'
    args: tuple[Resolved, ...] = ()
    key: EntityKey | None = None
    params: tuple[tuple[str, str], ...] = ()
    exact: str | None = None
    dependency: str = ''
    members: tuple[Resolved, ...] = field(default=(), compare=False, repr=False)
    exact_key: EntityKey | None = field(default=None, compare=False, repr=False)
    exact_value: FiniteValue | None = None
    allowed_values: frozenset[FiniteValue] = frozenset()

    def base(self) -> Resolved:
        """Remove a singleton constraint.

        :returns: The underlying type, preserving other constraints.
        """
        return replace(
            self, exact=None, exact_key=None, exact_value=None, allowed_values=frozenset()
        )

    def string_like(self) -> bool:
        """Recognize defined one-way string conversions.

        :returns: Whether the type can be used in a string position.
        """
        return self.kind == 'reference' or self.name in (
            'string',
            'snowflake_id',
            'code',
            'type',
            'attrs',
        )


UNKNOWN = Resolved('?', 'unknown')
ENTITY = Resolved('Entity', 'reference', key=('', 'Entity'))


def singleton(type_: Resolved) -> FiniteValue | None:
    """Convert an existing singleton contract into a typed finite member.

    :param type_: Resolved type, possibly constrained by ``exact``.
    :returns: Typed singleton, or None if no supported singleton is present.
    """
    if type_.exact_value is not None:
        return type_.exact_value
    value = type_.exact
    if value is None or type_.kind == 'reference':
        return None
    if type_.name == 'bool':
        payload: str | int | bool | Decimal | float | date | datetime = value == 'true'
    elif type_.name in ('integer', 'snowflake_id'):
        payload = int(value)
    elif type_.name == 'decimal':
        payload = Decimal(value)
    elif type_.name == 'float':
        payload = float(value)
    elif type_.name == 'date':
        payload = date.fromisoformat(value)
    elif type_.name == 'datetime':
        payload = datetime.fromisoformat(value)
    else:
        payload = value
    target = type_.args[0].key if type_.name == 'code' else type_.key
    return FiniteValue(type_.name, target, payload)


def portable_flags(type_: Resolved, world: World) -> Value:
    """Encode an already checked flags singleton with its canonical mask.

    :param type_: Code type with a normalized selected-code singleton.
    :param world: Declaration index containing its flags dictionary.
    :returns: Typed portable flags value.
    """
    key = type_.args[0].key
    declaration = world.declarations[key or ('', '')][1]
    selected = tuple(filter(None, (type_.exact or '').split('+')))
    mask = sum(
        int(str(tokens(entry)[1]), 0) if '0b' in str(tokens(entry)[1]) else int(tokens(entry)[1])
        for entry in children(declaration, 'entry')
        if str(tokens(entry)[2]) in selected
    )
    return Value('flags', '', qualified(key) if key else '', selected, str(mask))


def accepts(expected: Resolved, actual: Resolved, world: World) -> bool:
    """Check a value against an existing contract without widening it.

    :param expected: Required type.
    :param actual: Supplied value's type, optionally carrying a singleton.
    :param world: Entity ancestor index.
    :returns: Whether the supplied value meets the contract.
    """
    if 'unknown' in (expected.kind, actual.kind):
        return True
    if expected.allowed_values:
        if not accepts(expected.base(), actual.base(), world):
            return False
        values = actual.allowed_values
        if not values:
            only = singleton(actual)
            if only is None:
                return False
            values = frozenset((only,))
        if expected.name == 'string':
            values = frozenset(
                value if value.kind == 'string' else FiniteValue('string', None, str(value.value))
                for value in values
            )
        return values <= expected.allowed_values
    if expected.exact is not None:
        if expected.kind == 'reference':
            return expected == actual
        return accepts(expected.base(), actual.base(), world) and singleton(expected) == singleton(
            actual
        )
    if expected == actual.base():
        return True
    if expected.name == 'string' and not expected.params and actual.string_like():
        return True
    if expected.kind == actual.kind == 'reference':
        return expected == ENTITY or expected.key in world.parents.get(actual.key or ('', ''), [])
    if expected.name == actual.name == 'type':
        return accepts(expected.args[0], actual.args[0], world)
    return False


def common_type(types: list[Resolved], world: World) -> Resolved | None:
    """Find a common overload contract using only confirmed conversions.

    :param types: Independent branch types.
    :param world: Entity ancestor index.
    :returns: Common type, or None when no conversion is defined.
    """
    if not types:
        return None
    if all(item == types[0] for item in types):
        return types[0]
    result = types[0].base()
    for original in types[1:]:
        other = original.base()
        if result == other:
            continue
        if result.kind == other.kind == 'reference':
            candidates = [result.key, *world.parents.get(result.key or ('', ''), [])]
            ancestors = [other.key, *world.parents.get(other.key or ('', ''), [])]
            shared = next((key for key in candidates if key in ancestors), None)
            result = Resolved(shared[1], 'reference', key=shared) if shared else ENTITY
        elif result.name == other.name == 'type':
            target = common_type([result.args[0], other.args[0]], world)
            if target is None:
                return None
            result = Resolved('type', args=(target,))
        elif result.string_like() and other.string_like():
            result = Resolved('string', 'string')
        else:
            return None
    return result


def check_type(
    node: Tree[Token],
    unit: Unit,
    diagnostics: list[Diagnostic],
    self_fields: FieldTypes | None = None,
    allow_exact: bool = False,
) -> Kind:
    """Resolve an expression and report invalid types.

    :param node: Type expression.
    :param unit: Lexical scope.
    :param diagnostics: Destination for positioned diagnostics.
    :param self_fields: Fields of the current instance.
    :param allow_exact: Whether this is a marked field declaration.
    :returns: The expanded type category.
    """
    from yambl_compiler.languages.yambl.semantics.expressions import Resolver

    return Resolver(unit, diagnostics, self_fields or {}, allow_exact).resolve(node).kind
