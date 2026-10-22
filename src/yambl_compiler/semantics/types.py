"""Validate value, reference, collection and external-code type expressions."""

from typing import Literal

from lark import Token, Tree

from yambl_compiler.semantics.symbols import ENTITIES, GENERICS, VALUES, Unit
from yambl_compiler.syntax.parser import Diagnostic, children, diagnostic, string_value, tokens

type Kind = Literal['value', 'string', 'reference', 'collection', 'unknown']


def check_type(node: Tree[Token], unit: Unit, diagnostics: list[Diagnostic]) -> Kind:
    """Check a type within its declaring file's scope.

    :param node: Type syntax or generic argument.
    :param unit: Source and lexical scope.
    :param diagnostics: Destination for errors and unresolved Decodes warnings.
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
    if name in GENERICS:
        args = (
            [child for child in arguments[0].children if isinstance(child, Tree)]
            if arguments
            else []
        )
        if parameters or len(args) != (2 if name == 'map' else 1):
            issue(f'invalid type arguments for {name}')
            return 'unknown'
        if name == 'code':
            argument = tokens(args[0])
            if len(argument) != 1:
                issue('code requires a Decodes instance identifier')
            else:
                identifier = (
                    string_value(argument[0]) if argument[0].type == 'STRING' else str(argument[0])
                )
                symbol = unit.scope.get(identifier)
                if (
                    symbol is not None and symbol.kind != 'decodes'
                ) or identifier in ENTITIES | VALUES | GENERICS:
                    issue('code requires a Decodes instance identifier')
                elif symbol is None:
                    issue(f'undefined Decodes: {identifier}', 'warning')
            return 'value'
        kinds = [check_type(arg, unit, diagnostics) for arg in args]
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
