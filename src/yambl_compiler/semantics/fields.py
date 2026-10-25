"""Validate field modifiers, descriptive annotations and configuration defaults."""

import re

from lark import Token, Tree

from yambl_compiler.semantics.expressions import Resolver
from yambl_compiler.semantics.symbols import Unit
from yambl_compiler.semantics.types import FieldTypes, check_type
from yambl_compiler.syntax.parser import (
    Diagnostic,
    annotations,
    children,
    diagnostic,
    string_value,
    tokens,
)


def field_name(body: Tree[Token]) -> str:
    """Read the field identifier before its colon.

    :param body: Field body, optionally including a default.
    :returns: Field name independent of modifier or default presence.
    """
    return str(next(token for token in tokens(body) if token.type == 'NAME'))


def check_field(
    field: Tree[Token],
    unit: Unit,
    diagnostics: list[Diagnostic],
    extension: bool,
    self_fields: FieldTypes | None = None,
) -> None:
    """Check field syntax semantics without inspecting business records.

    :param field: Field with zero or more annotations.
    :param unit: Owning source and scope.
    :param diagnostics: Destination for type and modifier diagnostics.
    :param extension: Whether this field adds an attribute to an existing entity.
    :param self_fields: Effective fields for dependent mapping types.
    :returns: None; appends errors and unresolved Decodes warnings.
    """
    body = children(field, 'field_body')[0]
    mode = str(tokens(body)[0])
    name = field_name(body)
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', name):
        diagnostics.append(diagnostic(unit.document, body, 'field names must use snake_case'))
    if extension and mode == 'id':
        diagnostics.append(
            diagnostic(unit.document, body, 'extend may add attributes, not id fields')
        )
    type_node = children(body, 'type_expr')[0]
    if any(str(token) == 'input_type' for token in tokens(type_node)):
        diagnostics.append(
            diagnostic(unit.document, type_node, 'input_type is only a decorator pair constraint')
        )
    if any(str(token) == "dimension_ref" for token in tokens(type_node)):
        diagnostics.append(
            diagnostic(
                unit.document,
                type_node,
                "dimension_ref is only a metadata declaration reference",
            )
        )
    check_type(
        type_node,
        unit,
        diagnostics,
        self_fields,
        bool(annotations(field, 'overload', 'override')),
    )
    markers = annotations(field, 'overload', 'override')
    if len(markers) > 1:
        diagnostics.append(
            diagnostic(
                unit.document,
                markers[-1],
                'only one overload or override marker is allowed',
            )
        )
    for default in children(body, 'default_value'):
        if mode != 'config':
            diagnostics.append(
                diagnostic(
                    unit.document,
                    default,
                    'defaults are currently supported only on config attr',
                )
            )
        resolved_default = Resolver(unit, [], self_fields or {}, bool(markers)).resolve(type_node)
        type_name = resolved_default.name
        literal = tokens(default)[1]
        allowed = {
            'integer': {'INT'},
            'snowflake_id': {'INT'},
            'float': {'INT', 'NUMBER'},
            'decimal': {'INT', 'NUMBER'},
            'string': {'STRING'},
            'bool': {'BOOLEAN'},
        }
        value = string_value(literal) if literal.type == 'STRING' else str(literal)
        if literal.type not in allowed.get(type_name, set()) or (
            resolved_default.exact is not None and value != resolved_default.exact
        ):
            diagnostics.append(
                diagnostic(
                    unit.document,
                    default,
                    f"unsupported or incompatible config default for {type_name}",
                )
            )
