"""Compare field contracts by resolved names and normalized annotation values."""

from lark import Token, Tree

from yambl_compiler.languages.yambl.semantics.decorators import bind_call
from yambl_compiler.languages.yambl.semantics.symbols import Unit
from yambl_compiler.languages.yambl.semantics.types import FieldTypes
from yambl_compiler.languages.yambl.syntax.parser import annotations, children, field_mode, tokens


def type_signature(node: Tree[Token], unit: Unit, self_fields: FieldTypes) -> str:
    """Normalize a type without changing its source spelling.

    :param node: Type or generic argument.
    :param unit: Declaring scope.
    :param self_fields: Effective fields for dependent references.
    :returns: Comparable structural representation.
    """
    from yambl_compiler.languages.yambl.semantics.expressions import Resolver

    return repr(Resolver(unit, [], self_fields, True).resolve(node))


def field_signature(
    field: Tree[Token],
    unit: Unit,
    self_fields: FieldTypes,
    include_type: bool = True,
    ignore_extra_input: bool = False,
) -> str:
    """Normalize a field contract, excluding its default and relationship marker.

    :param field: Field declaration.
    :param unit: Declaring scope.
    :param self_fields: Effective fields of its enclosing entity.
    :param include_type: Whether to include the resolved type contract.
    :param ignore_extra_input: Whether inferred calculation inputs are compared separately.
    :returns: Signature suitable for overload and override comparisons.
    """
    body = children(field, 'field_body')[0]
    metadata = [
        bind_call(call, field, unit, [])
        for call in annotations(field)
        if str(tokens(call)[1]) not in ('override', 'overload', 'sparse', 'dense')
        and not (ignore_extra_input and str(tokens(call)[1]) == 'extra_input_param')
    ]
    return repr(
        (
            field_mode(body),
            (
                type_signature(children(body, 'type_expr')[0], unit, self_fields)
                if include_type
                else ''
            ),
            sorted(metadata),
        )
    )
