"""Represent resolved types and explicitly supported compatibility rules."""

from dataclasses import dataclass, field, replace
from typing import Literal

from lark import Token, Tree

from yambl_compiler.semantics.symbols import EntityKey, Unit, World
from yambl_compiler.syntax.parser import Diagnostic

type Kind = Literal['value', 'string', 'reference', 'collection', 'unknown']
type FieldTypes = dict[str, tuple[Unit, Tree[Token]]]
MODES = {'attr', 'static', 'config', 'snapshot', 'scd', "computed"}


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

    def base(self) -> Resolved:
        """Remove a singleton constraint.

        :returns: The underlying type, preserving other constraints.
        """
        return replace(self, exact=None, exact_key=None)

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


def accepts(expected: Resolved, actual: Resolved, world: World) -> bool:
    """Check a value against an existing contract without widening it.

    :param expected: Required type.
    :param actual: Supplied value's type, optionally carrying a singleton.
    :param world: Entity ancestor index.
    :returns: Whether the supplied value meets the contract.
    """
    if 'unknown' in (expected.kind, actual.kind):
        return True
    if expected.exact is not None:
        return expected == actual
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
    from yambl_compiler.semantics.expressions import Resolver

    return Resolver(unit, diagnostics, self_fields or {}, allow_exact).resolve(node).kind
