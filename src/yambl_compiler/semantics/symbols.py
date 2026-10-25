"""Collect declarations and resolve file-local imports."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from lark import Token, Tree

from yambl_compiler.syntax.parser import (
    Diagnostic,
    Document,
    children,
    diagnostic,
    tokens,
)

VALUES = {
    'bool',
    'string',
    'integer',
    'float',
    'decimal',
    'date',
    'datetime',
    'snowflake_id',
    "dimension_ref",
}
GENERICS = {
    'collection',
    'values',
    'mapping',
    'code',
    'type',
    'attrs',
    'alias',
    'exact',
}
type EntityKey = tuple[str, str]
type DeclarationKind = Literal['entity', 'decodes', 'value', 'type_alias', 'decorator']


@dataclass(frozen=True)
class Symbol:
    """Identify a declaration independently of its importing file.

    :param key: Declaring namespace and name.
    :param kind: Entity type, structured value type, or Decodes instance.
    """

    key: EntityKey
    kind: DeclarationKind


@dataclass
class World:
    """Share declarations and effective fields across lexical scopes.

    :param declarations: Resolved declaration sites.
    :param fields: Effective field declarations by entity or value identity.
    :param parents: Nearest-first business ancestors, excluding Entity.
    :param reserved: Names provided by the selected language definitions.
    :param constants: Result constant meanings from the selected definitions.
    :param dimensions: Entity-scoped named dimension declarations.
    """

    declarations: dict[EntityKey, tuple[Unit, Tree[Token]]] = field(
        default_factory=lambda: dict[EntityKey, tuple[Unit, Tree[Token]]]()
    )
    fields: dict[EntityKey, dict[str, tuple[Unit, Tree[Token]]]] = field(
        default_factory=lambda: dict[EntityKey, dict[str, tuple[Unit, Tree[Token]]]]()
    )
    parents: dict[EntityKey, list[EntityKey]] = field(
        default_factory=dict[EntityKey, list[EntityKey]]
    )
    reserved: set[str] = field(default_factory=set[str])
    constants: dict[str, str] = field(default_factory=dict[str, str])
    dimensions: dict[EntityKey, dict[str, tuple[Unit, Tree[Token]]]] = field(
        default_factory=lambda: dict[EntityKey, dict[str, tuple[Unit, Tree[Token]]]]()
    )


@dataclass(frozen=True)
class Unit:
    """Pair parsed source with its visible declarations.

    :param document: Parsed source.
    :param scope: Own namespace declarations and file-local imports.
    :param world: Shared resolved declaration and field index.
    """

    document: Document
    scope: dict[str, Symbol]
    world: World = field(default_factory=World)


def namespace(document: Document) -> str:
    """Read a file's complete namespace including its repository prefix.

    :param document: Parsed source.
    :returns: Complete namespace, or the empty namespace.
    """
    declarations = children(document.tree, 'namespace_decl')
    local = ''.join(tokens(declarations[0])[1:]) if declarations else ''
    return '.'.join(part for part in (document.prefix, local) if part)


def symbol_key(document: Document, name: str, local: str | None = None) -> EntityKey:
    """Construct a repository-qualified symbol identity.

    :param document: Declaring or importing document.
    :param name: Symbol name.
    :param local: Relative import namespace, or None for the document namespace.
    :returns: Repository and namespace identity paired with the symbol name.
    """
    ns = (
        namespace(document) if local is None else '.'.join(p for p in (document.prefix, local) if p)
    )
    return (document.repository + '\0' + ns if document.repository else ns, name)


def header(node: Tree[Token]) -> Tree[Token]:
    """Select the header of an entity, extension, or Decodes declaration.

    :param node: Block declaration.
    :returns: Header excluding metadata and body.
    """
    return children(node, f"{node.data}_header")[0]


def declaration_name(node: Tree[Token]) -> str:
    """Read a declaration name independently of header modifiers.

    :param node: Entity, extension or dictionary declaration.
    :returns: Declared name.
    """
    return str(next(token for token in tokens(header(node)) if token.type == 'NAME'))


def field_owner(unit: Unit, syntax: Tree[Token]) -> Symbol | None:
    """Find the declaration owning an original field type expression.

    :param unit: Field's lexical scope.
    :param syntax: Field type syntax.
    :returns: Owning declaration, including the target of an extension.
    """
    for kind in ('entity', 'extension', 'value'):
        for declaration in children(unit.document.tree, kind):
            for field_node in children(declaration, 'field'):
                body = children(field_node, 'field_body')[0]
                if children(body, 'type_expr')[0] is syntax:
                    return unit.scope.get(declaration_name(declaration))
    return None


def resolve_scopes(
    documents: Sequence[Document],
    diagnostics: list[Diagnostic],
    builtin_documents: Sequence[Document] | None = None,
    constants: dict[str, str] | None = None,
) -> list[Unit]:
    """Resolve declarations before imports so file order does not affect visibility.

    :param documents: Complete compilation unit.
    :param diagnostics: Destination for name errors.
    :param builtin_documents: Embedded definitions, or None for installed resources.
    :param constants: Embedded constant meanings, required with embedded definitions.
    :returns: Sources with independent lexical scopes.
    """
    if builtin_documents is None:
        from yambl_compiler.semantics.builtins import DOCUMENTS, RESULT_CONSTANTS

        builtin_documents, constants = DOCUMENTS, RESULT_CONSTANTS
    declarations = {
        declaration_name(node): node
        for doc in builtin_documents
        for kind in ('entity', 'value', 'decodes', 'type_alias', 'decorator')
        for node in children(doc.tree, kind)
    }
    reserved = VALUES | GENERICS | set(declarations) | set(constants or {}) | {'Entity'}
    symbols: dict[EntityKey, Symbol] = {}
    world = World(reserved=reserved, constants=dict(constants or {}))
    for document in documents:
        for declaration in [
            *children(document.tree, 'entity'),
            *children(document.tree, 'decodes'),
            *children(document.tree, 'value'),
            *children(document.tree, 'type_alias'),
            *children(document.tree, 'decorator'),
        ]:
            name = declaration_name(declaration)
            key = symbol_key(document, name)
            pattern = (
                r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*'
                if declaration.data in ('value', 'type_alias', 'decorator')
                else r'[A-Z][a-zA-Z0-9]*'
            )
            if not re.fullmatch(pattern, name):
                diagnostics.append(
                    diagnostic(
                        document,
                        header(declaration),
                        (
                            (
                                'decorator names must use snake_case'
                                if declaration.data == 'decorator'
                                else 'value names must use snake_case'
                            )
                            if declaration.data in ('value', 'type_alias', 'decorator')
                            else 'declaration names must use PascalCase'
                        ),
                    )
                )
            model_definition = namespace(document) == 'builtins' and name in declarations
            if (name in reserved and not model_definition) or key in symbols:
                diagnostics.append(
                    diagnostic(
                        document,
                        header(declaration),
                        f"duplicate or reserved declaration: {name}",
                    )
                )
            kind = cast(DeclarationKind, declaration.data)
            symbols[key] = Symbol(key, kind)
    units: list[Unit] = []
    for document in documents:
        scope = {
            name: symbol
            for (ns, name), symbol in symbols.items()
            if ns == symbol_key(document, '')[0]
        }
        for imported in children(document.tree, 'import_decl'):
            ns = ''.join(tokens(children(imported, 'qualified_name')[0]))
            name = str(tokens(imported)[-1])
            target = symbols.get(symbol_key(document, name, ns))
            if target is None:
                diagnostics.append(diagnostic(document, imported, f"unknown import: {ns}.{name}"))
            elif name in scope or name in reserved:
                diagnostics.append(
                    diagnostic(
                        document,
                        imported,
                        f"import conflicts with visible name: {name}",
                    )
                )
            else:
                scope[name] = target
        units.append(Unit(document, scope, world))
    builtin_scope = {
        name: Symbol(('builtins', name), cast(DeclarationKind, node.data))
        for name, node in declarations.items()
    }
    for document in builtin_documents:
        builtin = Unit(document, builtin_scope, world)
        for node in document.tree.children:
            if isinstance(node, Tree) and node.data in (
                'entity',
                'value',
                'decodes',
                'type_alias',
                'decorator',
            ):
                world.declarations[('builtins', declaration_name(node))] = (
                    builtin,
                    node,
                )
    for unit in units:
        for node in unit.document.tree.children:
            if isinstance(node, Tree) and node.data in (
                'entity',
                'value',
                'decodes',
                'type_alias',
                'decorator',
            ):
                world.declarations[symbol_key(unit.document, declaration_name(node))] = (unit, node)
        for name, symbol in builtin_scope.items():
            unit.scope.setdefault(name, symbol)
    return units
