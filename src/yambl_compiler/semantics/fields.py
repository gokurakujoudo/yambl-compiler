"""Validate field modifiers, descriptive annotations and configuration defaults."""

import re

from lark import Token, Tree

from yambl_compiler.semantics.symbols import Unit
from yambl_compiler.semantics.types import FieldTypes, check_type
from yambl_compiler.syntax.parser import Diagnostic, children, diagnostic, tokens


def field_name(body: Tree[Token]) -> str:
    """Read the field identifier before its colon.

    :param body: Field body, optionally including a default.
    :returns: Field name independent of modifier or default presence.
    """
    return str(next(token for token in tokens(body) if token.type == 'NAME'))


def check_metadata(node: Tree[Token], unit: Unit, diagnostics: list[Diagnostic]) -> None:
    """Reject repeated title or description annotations on the same declaration.

    :param node: Entity, Decodes, or field declaration.
    :param unit: Owning source and scope.
    :param diagnostics: Destination for metadata errors.
    :returns: None; appends diagnostics.
    """
    seen: set[str] = set()
    for metadata in children(node, 'metadata'):
        name = str(tokens(metadata)[1])
        if name in seen:
            diagnostics.append(diagnostic(unit.document, metadata, f'duplicate metadata: {name}'))
        seen.add(name)


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
    check_type(type_node, unit, diagnostics, self_fields)
    markers = children(field, 'field_marker')
    if len(markers) > 1:
        diagnostics.append(
            diagnostic(
                unit.document, markers[-1], 'only one overload or override marker is allowed'
            )
        )
    if markers and mode == 'id':
        diagnostics.append(diagnostic(unit.document, markers[0], 'field markers require an attr'))
    check_metadata(field, unit, diagnostics)
    for temporal in children(field, 'temporal'):
        if mode not in ('snapshot', 'scd') or [str(t) for t in tokens(temporal)] != [
            '@',
            'temporal',
            '(',
            'as_of_date',
            ':',
            'date',
            ')',
        ]:
            diagnostics.append(
                diagnostic(
                    unit.document,
                    temporal,
                    'supported annotation is @temporal(as_of_date: date) on snapshot or scd attr',
                )
            )
    if len(children(field, 'temporal')) > 1:
        diagnostics.append(diagnostic(unit.document, field, 'duplicate temporal annotation'))
    dimensions_seen: set[str] = set()
    for annotation in children(field, 'dimensions'):
        for dimension in children(annotation, 'dimension'):
            dimension_name = str(tokens(dimension)[0])
            if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', dimension_name):
                diagnostics.append(
                    diagnostic(unit.document, dimension, 'dimension names must use snake_case')
                )
            if dimension_name in dimensions_seen:
                diagnostics.append(
                    diagnostic(unit.document, dimension, f'duplicate dimension: {dimension_name}')
                )
            dimensions_seen.add(dimension_name)
            check_type(children(dimension, 'type_expr')[0], unit, diagnostics, self_fields)
    for default in children(body, 'default_value'):
        if mode != 'config':
            diagnostics.append(
                diagnostic(
                    unit.document, default, 'defaults are currently supported only on config attr'
                )
            )
        type_name = str(tokens(type_node)[0])
        literal = tokens(default)[1]
        allowed = {
            'integer': {'INT'},
            'snowflake_id': {'INT'},
            'float': {'INT', 'NUMBER'},
            'decimal': {'INT', 'NUMBER'},
            'string': {'STRING'},
            'bool': {'BOOLEAN'},
        }
        if literal.type not in allowed.get(type_name, set()):
            diagnostics.append(
                diagnostic(
                    unit.document,
                    default,
                    f'unsupported or incompatible config default for {type_name}',
                )
            )
