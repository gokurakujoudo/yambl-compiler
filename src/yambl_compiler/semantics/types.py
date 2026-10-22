"""Validate value, reference, collection and external-code type expressions."""

from typing import Literal

from lark import Token, Tree

from yambl_compiler.semantics.symbols import ENTITIES, GENERICS, RESERVED, VALUES, Unit
from yambl_compiler.syntax.parser import Diagnostic, children, diagnostic, string_value, tokens

type Kind = Literal['value', 'string', 'reference', 'collection', 'unknown']
type FieldTypes = dict[str, tuple[Unit, Tree[Token]]]
MODES = {'attr', 'static', 'config', 'snapshot', 'scd'}


def entity_argument(node: Tree[Token], unit: Unit, universal: bool = False) -> bool:
    """Recognize a bare entity type constraint.

    :param node: Generic argument.
    :param unit: Argument's lexical scope.
    :param universal: Whether the Entity constraint is permitted.
    :returns: Whether the argument denotes an entity constraint.
    """
    items = tokens(node)
    name = str(items[0])
    symbol = unit.scope.get(name)
    return (
        node.data == 'type_expr'
        and len(items) == 1
        and (
            (universal and name == 'Entity')
            or name in ENTITIES
            or (symbol is not None and symbol.kind == 'entity')
        )
    )


def check_type(
    node: Tree[Token],
    unit: Unit,
    diagnostics: list[Diagnostic],
    self_fields: FieldTypes | None = None,
) -> Kind:
    """Check a type within its declaring file's scope.

    :param node: Type syntax or generic argument.
    :param unit: Source and lexical scope.
    :param diagnostics: Destination for errors and unresolved Decodes warnings.
    :param self_fields: Effective fields of the enclosing entity, with declaring scopes.
    :returns: Type category, or unknown after an invalid reference.
    """
    name = str(tokens(node)[0])
    parameters = children(node, 'parameters')
    arguments = children(node, 'arguments')

    def issue(message: str, severity: Literal['error', 'warning'] = 'error') -> None:
        """Report a problem at this type expression.

        :param message: Explanation.
        :param severity: Error or nonfatal warning.
        :returns: None; appends the diagnostic.
        """
        diagnostics.append(diagnostic(unit.document, node, message, severity))

    if node.data == 'string_argument':
        issue('quoted dictionary identifiers are only valid inside code<...>')
        return 'unknown'
    if node.data == 'self_field':
        issue('self fields are only valid as field_mapping targets')
        return 'unknown'
    if name in GENERICS:
        args = (
            [child for child in arguments[0].children if isinstance(child, Tree)]
            if arguments
            else []
        )
        if parameters or len(args) != (2 if name in ('map', 'field_mapping') else 1):
            issue(f'invalid type arguments for {name}')
            return 'unknown'
        if name == 'type':
            if not entity_argument(args[0], unit, universal=True):
                issue('type requires Entity or a visible entity type')
            return 'value'
        if name == 'field_mapping':
            if (
                args[0].data != 'type_expr'
                or len(tokens(args[0])) != 1
                or str(tokens(args[0])[0]) not in MODES
            ):
                issue('field_mapping requires an attr, static, config, snapshot or scd filter')
            target = args[1]
            if target.data == 'self_field':
                field_name = str(tokens(target)[-1])
                field = (self_fields or {}).get(field_name)
                if field is None:
                    issue(f'unknown self field: {field_name}')
                else:
                    scope, field_type = field
                    field_args = children(field_type, 'arguments')
                    constraint = children(field_args[0], 'type_expr') if field_args else []
                    if (
                        str(tokens(field_type)[0]) != 'type'
                        or len(constraint) != 1
                        or len(tokens(field_type)) != 4
                        or not entity_argument(constraint[0], scope, universal=True)
                    ):
                        issue(f'self field must have a valid type<...> type: {field_name}')
            elif not entity_argument(target, unit):
                issue('field_mapping requires a visible entity type or self field target')
            return 'collection'
        if name == 'code':
            argument = tokens(args[0])
            if len(argument) != 1:
                issue('code requires a Decodes instance identifier')
            else:
                identifier = (
                    string_value(argument[0]) if argument[0].type == 'STRING' else str(argument[0])
                )
                symbol = unit.scope.get(identifier)
                if (symbol is not None and symbol.kind != 'decodes') or identifier in RESERVED:
                    issue('code requires a Decodes instance identifier')
                elif symbol is None:
                    issue(f'undefined Decodes: {identifier}', 'warning')
            return 'value'
        kinds = [check_type(arg, unit, diagnostics, self_fields) for arg in args]
        if name == 'collection' and kinds[0] not in ('reference', 'unknown'):
            issue('collection requires an entity reference')
        if name == 'values' and kinds[0] not in ('value', 'string', 'unknown'):
            issue('values requires a value type')
        if name == 'map':
            if kinds[0] not in ('string', 'reference', 'unknown'):
                issue('map keys must be string or entity references')
            if kinds[1] == 'collection':
                issue('map values cannot be collections')
        return 'collection'
    if arguments:
        issue(f'{name} does not accept generic arguments')
    if parameters:
        parameter_tokens = tokens(parameters[0])[1:-1]
        named = children(parameters[0], 'named_parameter')
        if name == 'string':
            valid = len(parameter_tokens) == 1 and parameter_tokens[0].type == 'STRING'
            valid = valid or (
                len(parameter_tokens) == 3
                and str(parameter_tokens[0]) == 'regex'
                and parameter_tokens[-1].type == 'STRING'
            )
        elif name == 'decimal':
            valid = len(parameter_tokens) == 3 and [t.type for t in parameter_tokens] == [
                'INT',
                'COMMA',
                'INT',
            ]
            valid = valid or (
                len(parameter_tokens) == 7
                and len(named) == 2
                and {str(tokens(n)[0]) for n in named} == {'precision', 'scale'}
                and all(tokens(n)[-1].type == 'INT' for n in named)
            )
        else:
            valid = False
        if not valid:
            issue(f'invalid parameters for {name}; do not mix positional and named parameters')
    if name in VALUES:
        return 'string' if name == 'string' else 'value'
    symbol = unit.scope.get(name)
    if name in ENTITIES or (symbol is not None and symbol.kind == 'entity'):
        return 'reference'
    if symbol is not None:
        issue(f'{name} is an instance; use code<{name}> or Decodes')
    else:
        issue(f'unknown type: {name}')
    return 'unknown'
