"""Resolve file-local imports and validate the currently defined type rules."""

import re
from collections.abc import Sequence
from typing import Literal

from lark import Token, Tree

from yambl_compiler.syntax.parser import Diagnostic, Document, children, tokens

VALUES = {'bool', 'string', 'integer', 'float', 'decimal', 'date', 'datetime', 'snowflake_id'}
GENERICS = {'collection', 'values', 'map', 'code'}
ENTITIES = {'Decodes', 'DecodesEntry'}
type Kind = Literal['value', 'string', 'reference', 'collection', 'unknown']


def namespace(document: Document) -> str:
    """Read a file's declared namespace.

    :param document: Parsed source.
    :returns: Declared name, or the unnamed namespace for standalone fragments.
    """
    declarations = children(document.tree, 'namespace_decl')
    return ''.join(tokens(declarations[0])[1:]) if declarations else ''


def check(documents: Sequence[Document]) -> list[Diagnostic]:
    """Validate declarations, imports, and field type categories.

    :param documents: Complete compilation unit, including reference dependencies.
    :returns: Source-ordered errors.
    """
    diagnostics: list[Diagnostic] = []
    symbols: dict[tuple[str, str], str] = {}

    def issue(document: Document, node: Tree[Token], message: str) -> None:
        """Append a diagnostic at the first token of a rule.

        :param document: Owning source.
        :param node: Offending syntax.
        :param message: Explanation.
        :returns: None; appends to the result list.
        """
        token = tokens(node)[0]
        diagnostics.append(Diagnostic(document.path, token.line or 1, token.column or 1, message))

    for document in documents:
        for declaration in document.tree.children:
            if isinstance(declaration, Tree) and declaration.data in ('entity', 'decodes'):
                name = str(tokens(declaration)[1])
                key = (namespace(document), name)
                if not re.fullmatch(r'[A-Z][a-zA-Z0-9]*', name):
                    issue(document, declaration, 'declaration names must use PascalCase')
                if name in VALUES | GENERICS | ENTITIES or key in symbols:
                    issue(document, declaration, f'duplicate or reserved declaration: {name}')
                symbols[key] = str(declaration.data)

    for document in documents:
        scope = {name: kind for (ns, name), kind in symbols.items() if ns == namespace(document)}
        scope.update(dict.fromkeys(ENTITIES, 'entity'))
        for imported in children(document.tree, 'import_decl'):
            ns = ''.join(tokens(children(imported, 'qualified_name')[0]))
            name = str(tokens(imported)[-1])
            target = symbols.get((ns, name))
            if target is None:
                issue(document, imported, f'unknown import: {ns}.{name}')
            elif name in scope:
                issue(document, imported, f'import conflicts with visible name: {name}')
            else:
                scope[name] = target

        def type_kind(node: Tree[Token]) -> Kind:
            """Validate a type expression in this file's lexical scope.

            :param node: Type syntax subtree.
            :returns: Type category, or unknown after a diagnostic.
            """
            name = str(tokens(node)[0])
            parameters = children(node, 'parameters')
            arguments = children(node, 'arguments')
            if name in GENERICS:
                args = children(arguments[0], 'type_expr') if arguments else []
                if parameters or len(args) != (2 if name == 'map' else 1):
                    issue(document, node, f'invalid type arguments for {name}')
                    return 'unknown'
                if name == 'code':
                    argument = tokens(args[0])
                    if len(argument) != 1 or scope.get(str(argument[0])) != 'decodes':
                        issue(document, node, 'code requires a visible Decodes instance')
                    return 'value'
                kinds = [type_kind(arg) for arg in args]
                if name == 'collection' and kinds[0] not in ('reference', 'unknown'):
                    issue(document, node, 'collection requires an entity reference')
                if name == 'values' and kinds[0] not in ('value', 'string', 'unknown'):
                    issue(document, node, 'values requires a value type')
                if name == 'map':
                    if kinds[0] not in ('string', 'reference', 'unknown'):
                        issue(document, node, 'map keys must be string or entity references')
                    if kinds[1] == 'collection':
                        issue(document, node, 'map values cannot be collections')
                return 'collection'
            if arguments:
                issue(document, node, f'{name} does not accept generic arguments')
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
                    issue(
                        document,
                        node,
                        f'invalid parameters for {name}; do not mix positional and named parameters',
                    )
            if name in VALUES:
                return 'string' if name == 'string' else 'value'
            if scope.get(name) == 'entity':
                return 'reference'
            if scope.get(name) == 'decodes':
                issue(document, node, f'{name} is an instance; use code<{name}> or Decodes')
            else:
                issue(document, node, f'unknown type: {name}')
            return 'unknown'

        for entity in children(document.tree, 'entity'):
            seen: set[str] = set()
            for field in children(entity, 'field'):
                body = children(field, 'field_body')[0]
                field_tokens = tokens(body)
                name = str(body.children[-3])
                if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', name):
                    issue(document, body, 'field names must use snake_case')
                if name in seen:
                    issue(document, body, f'duplicate field: {name}')
                seen.add(name)
                type_kind(children(body, 'type_expr')[0])
                for annotation in children(field, 'annotation'):
                    if field_tokens[0] != 'snapshot' or [str(t) for t in tokens(annotation)] != [
                        '@',
                        'temporal',
                        '(',
                        'as_of_date',
                        ':',
                        'date',
                        ')',
                    ]:
                        issue(
                            document,
                            annotation,
                            'supported annotation is @temporal(as_of_date: date) on snapshot attr',
                        )
        for decodes in children(document.tree, 'decodes'):
            codes: set[str] = set()
            for entry in children(decodes, 'entry'):
                code = str(tokens(entry)[2])
                if code in codes:
                    issue(document, entry, f'duplicate code in Decodes identity: {code}')
                codes.add(code)
    return sorted(
        diagnostics,
        key=lambda diagnostic: (str(diagnostic.path), diagnostic.line, diagnostic.column),
    )
