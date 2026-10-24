"""Compare field contracts by resolved names and normalized annotation values."""

from lark import Token, Tree

from yambl_compiler.semantics.decorators import bind_call
from yambl_compiler.semantics.symbols import Unit
from yambl_compiler.semantics.types import FieldTypes
from yambl_compiler.syntax.parser import annotations, children, tokens


def type_signature(node: Tree[Token], unit: Unit, self_fields: FieldTypes) -> str:
    """Normalize a type without changing its source spelling.

    :param node: Type or generic argument.
    :param unit: Declaring scope.
    :param self_fields: Effective fields for dependent references.
    :returns: Comparable structural representation.
    """
    from yambl_compiler.semantics.expressions import Resolver

    return repr(Resolver(unit, [], self_fields, True).resolve(node))


def field_signature(
    field: Tree[Token], unit: Unit, self_fields: FieldTypes, include_type: bool = True
) -> str:
    """Normalize a field contract, excluding its default and relationship marker.

    :param field: Field declaration.
    :param unit: Declaring scope.
    :param self_fields: Effective fields of its enclosing entity.
    :param include_type: Whether to include the resolved type contract.
    :returns: Signature suitable for overload and override comparisons.
    """
    body = children(field, 'field_body')[0]
    metadata = [
        bind_call(call, field, unit, [])
        for call in annotations(field)
        if str(tokens(call)[1]) not in ('override', 'overload')
    ]
    return repr(
        (
            str(tokens(body)[0]).replace('attr', 'static'),
            (
                type_signature(children(body, 'type_expr')[0], unit, self_fields)
                if include_type
                else ''
            ),
            sorted(metadata),
        )
    )
