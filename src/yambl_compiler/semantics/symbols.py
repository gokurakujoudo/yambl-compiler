"""Collect declarations and resolve file-local imports."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from lark import Token, Tree

from yambl_compiler.syntax.parser import Diagnostic, Document, children, diagnostic, tokens

VALUES = {'bool', 'string', 'integer', 'float', 'decimal', 'date', 'datetime', 'snowflake_id'}
GENERICS = {'collection', 'values', 'map', 'code'}
ENTITIES = {'Decodes', 'DecodesEntry'}
type EntityKey = tuple[str, str]


@dataclass(frozen=True)
class Symbol:
    """Identify a declaration independently of its importing file.

    :param key: Declaring namespace and name.
    :param kind: Entity type or Decodes instance.
    """

    key: EntityKey
    kind: Literal['entity', 'decodes']


@dataclass(frozen=True)
class Unit:
    """Pair parsed source with its visible declarations.

    :param document: Parsed source.
    :param scope: Own namespace declarations and file-local imports.
    """

    document: Document
    scope: dict[str, Symbol]


def namespace(document: Document) -> str:
    """Read a file's declared namespace.

    :param document: Parsed source.
    :returns: Declared name, or the unnamed namespace.
    """
    declarations = children(document.tree, 'namespace_decl')
    return ''.join(tokens(declarations[0])[1:]) if declarations else ''


def header(node: Tree[Token]) -> Tree[Token]:
    """Select the header of an entity, extension, or Decodes declaration.

    :param node: Block declaration.
    :returns: Header excluding metadata and body.
    """
    return children(node, f'{node.data}_header')[0]


def resolve_scopes(documents: Sequence[Document], diagnostics: list[Diagnostic]) -> list[Unit]:
    """Resolve declarations before imports so file order does not affect visibility.

    :param documents: Complete compilation unit.
    :param diagnostics: Destination for name errors.
    :returns: Sources with independent lexical scopes.
    """
    symbols: dict[EntityKey, Symbol] = {}
    for document in documents:
        for declaration in [
            *children(document.tree, 'entity'),
            *children(document.tree, 'decodes'),
        ]:
            name = str(tokens(header(declaration))[1])
            key = (namespace(document), name)
            if not re.fullmatch(r'[A-Z][a-zA-Z0-9]*', name):
                diagnostics.append(
                    diagnostic(
                        document, header(declaration), 'declaration names must use PascalCase'
                    )
                )
            if name in VALUES | GENERICS | ENTITIES or key in symbols:
                diagnostics.append(
                    diagnostic(
                        document, header(declaration), f'duplicate or reserved declaration: {name}'
                    )
                )
            symbols[key] = Symbol(key, 'entity' if declaration.data == 'entity' else 'decodes')
    units: list[Unit] = []
    for document in documents:
        scope = {
            name: symbol for (ns, name), symbol in symbols.items() if ns == namespace(document)
        }
        for imported in children(document.tree, 'import_decl'):
            ns = ''.join(tokens(children(imported, 'qualified_name')[0]))
            name = str(tokens(imported)[-1])
            target = symbols.get((ns, name))
            if target is None:
                diagnostics.append(diagnostic(document, imported, f'unknown import: {ns}.{name}'))
            elif name in scope or name in ENTITIES:
                diagnostics.append(
                    diagnostic(document, imported, f'import conflicts with visible name: {name}')
                )
            else:
                scope[name] = target
        units.append(Unit(document, scope))
    return units
