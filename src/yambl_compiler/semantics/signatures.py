"""Compare field contracts by resolved names and normalized annotation values."""

from lark import Token, Tree

from yambl_compiler.semantics.symbols import Unit
from yambl_compiler.semantics.types import FieldTypes
from yambl_compiler.syntax.parser import children, string_value, tokens


def type_signature(node: Tree[Token], unit: Unit, self_fields: FieldTypes) -> str:
    """Normalize a type without changing its source spelling.

    :param node: Type or generic argument.
    :param unit: Declaring scope.
    :param self_fields: Effective fields for dependent references.
    :returns: Comparable structural representation.
    """
    items = tokens(node)
    name = string_value(items[0]) if items[0].type == 'STRING' else str(items[0])
    if node.data == 'self_field':
        field_name = str(items[-1])
        field = self_fields.get(field_name)
        return repr(('self', field_name, type_signature(field[1], field[0], {}) if field else None))
    symbol = unit.scope.get(name)
    identity = repr(symbol.key) if symbol else name
    args = [
        type_signature(arg, unit, self_fields)
        for group in children(node, 'arguments')
        for arg in group.children
        if isinstance(arg, Tree)
    ]
    params: list[tuple[str, str]] = []
    for group in children(node, 'parameters'):
        position = 0
        for child in group.children:
            if isinstance(child, Tree):
                parameter = tokens(child)
                key, value = str(parameter[0]), parameter[-1]
            elif child.type in ('INT', 'STRING'):
                key = ('precision', 'scale')[min(position, 1)] if name == 'decimal' else 'regex'
                value = child
                position += 1
            else:
                continue
            if value.type == 'INT':
                normalized = str(value).lstrip('-0') or '0'
                if str(value).startswith('-') and normalized != '0':
                    normalized = '-' + normalized
            else:
                normalized = string_value(value)
            params.append((key, normalized))
    return repr((identity, args, sorted(params)))


def field_signature(field: Tree[Token], unit: Unit, self_fields: FieldTypes) -> str:
    """Normalize a field contract, excluding its default and relationship marker.

    :param field: Field declaration.
    :param unit: Declaring scope.
    :param self_fields: Effective fields of its enclosing entity.
    :returns: Signature suitable for overload and override comparisons.
    """
    body = children(field, 'field_body')[0]
    annotations: list[tuple[str, str]] = []
    for annotation in children(field, 'metadata'):
        items = tokens(annotation)
        annotations.append((str(items[1]), string_value(items[-2])))
    for kind in ('temporal', 'dimension_annotation'):
        for annotation in children(field, kind):
            parameters = [
                (
                    str(tokens(parameter)[0]),
                    type_signature(children(parameter, 'type_expr')[0], unit, self_fields),
                )
                for parameter in children(annotation, 'dimension')
            ]
            annotations.append((kind, repr(parameters)))
    return repr(
        (
            str(tokens(body)[0]),
            type_signature(children(body, 'type_expr')[0], unit, self_fields),
            sorted(annotations),
        )
    )
