"""Adapt embedded semantic expressions to the shared static checking engine."""

from pathlib import Path

from lark import Token, Tree

from yambl_compiler.ir.model import (
    Call,
    Definition,
    Dimension,
    Expression,
    Field,
    Model,
    Source,
    Value,
)
from yambl_compiler.languages.yambl.semantics.symbols import EntityKey, resolve_scopes
from yambl_compiler.languages.yambl.semantics.validation import Analysis, analyze_units
from yambl_compiler.languages.yambl.syntax.computation import restore_body, restore_expression
from yambl_compiler.languages.yambl.syntax.parser import Diagnostic, Document


def token(value: str, kind: str, source: Source) -> Token:
    """Create a semantic token with its retained original location.

    :param value: Semantic spelling.
    :param kind: Internal token category.
    :param source: Portable original span.
    :returns: Located checker token.
    """
    return Token(
        kind,
        value,
        start_pos=source.line * 1000000 + source.column,
        line=source.line,
        column=source.column,
        end_line=source.end_line,
        end_column=source.end_column,
    )


def scalar(value: Value, source: Source) -> Token:
    """Adapt a checked scalar without executing or constructing runtime values.

    :param value: IR literal.
    :param source: Original span.
    :returns: Scalar token.
    """
    if value.kind == 'string':
        return token(
            "'" + value.text.replace('\\', '\\\\').replace("'", "\\'") + "'",
            'STRING',
            source,
        )
    kind = {'integer': 'INT', 'decimal': 'NUMBER', 'bool': 'BOOLEAN'}[value.kind]
    return token(value.text, kind, source)


class Restoring:
    """Build checker inputs from IR records without loading source or builtins."""

    def __init__(self) -> None:
        """Initialize document-local resolved reference tables.

        :returns: None.
        """
        self.bindings: dict[str, EntityKey] = {}

    def reference(self, target: str, source: Source) -> Token:
        """Bind a qualified identity to its original short name.

        :param target: Qualified declaration identity.
        :param source: Reference span.
        :returns: Checker name token.
        :raises ValueError: If a lexical name maps to conflicting identities.
        """
        local_builtin = target.startswith('model::')
        namespace, _, name = target.removeprefix('model::').rpartition('.')
        if namespace != 'builtins' or local_builtin:
            namespace = 'model\0' + namespace
        key = (namespace, name)
        if name in self.bindings and self.bindings[name] != key:
            raise ValueError(f"{source.path}:{source.line}: conflicting lexical reference: {name}")
        self.bindings[name] = key
        return token(name, 'NAME', source)

    def expression(self, expression: Expression) -> Tree[Token]:
        """Adapt explicit semantic operations, not serialized parser trees.

        :param expression: Structured expression.
        :returns: Checker expression with resolved lexical bindings.
        """
        source = expression.source
        if expression.kind == 'reference':
            first = self.reference(expression.target, source)
        elif expression.kind == 'external':
            first = scalar(Value('string', expression.name, ''), source)
        elif expression.literal is not None:
            first = scalar(expression.literal, source)
        else:
            first = token(expression.name, 'SELF' if expression.kind == 'self' else 'NAME', source)
        parts: list[Token | Tree[Token]] = [first]
        if expression.arguments:
            group: list[Token | Tree[Token]] = [token('<', 'LESSTHAN', source)]
            for i, arg in enumerate(expression.arguments):
                if i:
                    group.append(token(',', 'COMMA', source))
                group.append(self.expression(arg))
            group.append(token('>', 'MORETHAN', source))
            parts.append(Tree('arguments', group))
        if expression.parameters:
            group = [token('[', 'LSQB', source)]
            for parameter in expression.parameters:
                group.append(
                    Tree(
                        'named_parameter',
                        [
                            token(parameter.name, 'NAME', source),
                            token(':', 'COLON', source),
                            scalar(
                                Value(
                                    ('string' if parameter.name == 'regex' else 'integer'),
                                    parameter.value,
                                    '',
                                ),
                                source,
                            ),
                        ],
                    )
                )
            group.append(token(']', 'RSQB', source))
            parts.append(Tree('parameters', group))
        for access in expression.accesses:
            parts.append(
                Tree(
                    'access',
                    [
                        token(access.operator, 'OP', access.source),
                        token(access.name, 'NAME', access.source),
                    ],
                )
            )
        for operation in expression.operations:
            parts.append(
                Tree(
                    'flag_operation',
                    [
                        token(operation.operator, 'OP', source),
                        Tree('flag_operand', self.expression(operation.operand).children),
                    ],
                )
            )
        if expression.binding:
            parts.append(
                Tree(
                    'binding',
                    [
                        token('as', 'AS', source),
                        token(expression.binding, 'NAME', source),
                    ],
                )
            )
        return Tree('type_expr', parts)

    def calls(self, calls: tuple[Call, ...]) -> list[Tree[Token]]:
        """Adapt metadata while retaining positional, named and pair distinctions.

        :param calls: Metadata occurrences.
        :returns: Checker annotations.
        """
        result: list[Tree[Token]] = []
        for call in calls:
            parts: list[Tree[Token] | Token] = [
                token('@', 'AT', call.source),
                self.reference(call.target, call.source),
            ]
            for arg in call.arguments:
                values: list[Tree[Token] | Token] = []
                if arg.kind != 'positional':
                    values.extend(
                        [
                            token(arg.name, 'NAME', arg.expression.source),
                            token(
                                ':' if arg.kind == 'pair' else '=',
                                'OP',
                                arg.expression.source,
                            ),
                        ]
                    )
                values.append(self.expression(arg.expression))
                if arg.default is not None:
                    values.append(
                        Tree(
                            'decorator_default',
                            [token('=', 'EQUAL', arg.default.source), self.expression(arg.default)],
                        )
                    )
                parts.append(Tree(arg.kind + '_argument', values))
            result.append(Tree('annotation', parts))
        return result

    def field(self, field: Field) -> Tree[Token]:
        """Restore a field implementation for shared validation.

        :param field: Field record.
        :returns: Checker field declaration.
        """
        parts: list[Token | Tree[Token]] = [
            token(field.category, field.category.upper(), field.source)
        ]
        if field.category not in ('id', 'attr'):
            parts.append(token('attr', 'ATTR', field.source))
        parts.extend(
            [
                token(field.name, 'NAME', field.source),
                token(':', 'COLON', field.source),
                self.expression(field.expression),
            ]
        )
        if field.default is not None:
            parts.append(
                Tree(
                    'default_value',
                    [
                        token('=', 'EQUAL', field.source),
                        scalar(field.default, field.source),
                    ],
                )
            )
        if field.calculation is not None:
            for reference in field.calculation.references:
                self.reference(reference.value, field.source)
            parts.append(restore_body(field.calculation.body))
        return Tree('field', [*self.calls(field.decorators), Tree('field_body', parts)])

    def declaration(
        self,
        definition: Definition,
        extension: tuple[Field, ...] | None = None,
        dimensions: tuple[Dimension, ...] = (),
    ) -> Document:
        """Restore a definition or one extension in its original lexical context.

        :param definition: Owning declaration.
        :param extension: Extension fields, or None for the declaration itself.
        :param dimensions: Dimensions declared in this lexical context.
        :returns: In-memory document; no source text is reconstructed or parsed.
        """
        source = definition.source
        if extension is not None:
            source = extension[0].source if extension else dimensions[0].source
        kind = 'extension' if extension is not None else definition.kind
        keyword = {'type_alias': 'type', 'extension': 'extend', 'constant': 'const'}.get(kind, kind)
        header_parts: list[Token | Tree[Token]] = []
        if definition.final and kind == 'entity':
            header_parts.append(token('final', 'FINAL', source))
        header_parts.extend(
            [
                token(keyword, keyword.upper(), source),
                self.reference(definition.identity, source),
            ]
        )
        if definition.parent and extension is None:
            parent = (
                token('Entity', 'NAME', source)
                if definition.parent == 'Entity'
                else self.reference(definition.parent, source)
            )
            header_parts.append(
                Tree('inheritance', [token('inherits', 'INHERITS', source), parent])
            )
        formals: list[Token | Tree[Token]] = []
        for parameter in (() if extension is not None else definition.parameters):
            p = parameter.source
            items: list[Token | Tree[Token]] = []
            if parameter.pair:
                items.append(token('*', 'STAR', p))
            items.append(token(parameter.name, 'NAME', p))
            if parameter.optional:
                items.append(token('?', 'QMARK', p))
            items.extend([token(':', 'COLON', p), self.expression(parameter.type)])
            if parameter.default:
                items.append(
                    Tree(
                        'decorator_default',
                        [token('=', 'EQUAL', p), self.expression(parameter.default)],
                    )
                )
            formals.append(
                Tree(
                    'decorator_parameter' if kind == 'decorator' else 'type_parameter',
                    items,
                )
            )
        if kind == 'decorator':
            header_parts.extend(formals)
        elif formals:
            header_parts.append(Tree('type_parameters', formals))
        # Header span is independent of parameter/source ordering.
        header_parts.append(token('', 'IR_END', source))
        body: list[Token | Tree[Token]] = [
            *self.calls(() if extension is not None else definition.decorators),
            Tree(kind + '_header', header_parts),
        ]
        if extension is None and definition.expression:
            body.append(self.expression(definition.expression))
        if extension is None and definition.initializer is not None:
            for reference in definition.references:
                self.reference(reference.value, definition.source)
            body.append(restore_expression(definition.initializer))
        if dimensions:
            for dimension in dimensions:
                p = dimension.source
                members: list[Token | Tree[Token]] = [
                    token("dimension", "DIMENSION", p),
                    token(dimension.name, "NAME", p),
                ]
                members.append(
                    Tree(
                        "dimension_value",
                        [
                            token("value", "VALUE", p),
                            token(dimension.value_name, "NAME", p),
                            token(":", "COLON", p),
                            self.expression(dimension.expression),
                        ],
                    )
                )
                if dimension.hierarchy_name:
                    hierarchy: list[Token | Tree[Token]] = [
                        token("hierarchy", "HIERARCHY", p),
                        token(dimension.hierarchy_name, "NAME", p),
                        token(":", "COLON", p),
                        Tree("type_expr", [self.reference("builtins.Hierarchy", p)]),
                    ]
                    if dimension.hierarchy_default is not None:
                        hierarchy.append(restore_expression(dimension.hierarchy_default))
                    members.append(Tree("dimension_hierarchy", hierarchy))
                if dimension.fallback is not None:
                    members.append(
                        Tree(
                            "dimension_fallback",
                            [
                                token("fallback_days", "FALLBACK_DAYS", p),
                                token("=", "EQUAL", p),
                                restore_expression(dimension.fallback),
                            ],
                        )
                    )
                members.append(token("}", "RBRACE", p))
                body.append(Tree("dimension", members))
        body.extend(
            self.field(f)
            for f in (
                extension
                if extension is not None
                else tuple(f for f in definition.fields if not f.extension and not f.builtin)
            )
        )
        for entry in (() if extension is not None else definition.entries):
            if not entry.generated:
                body.append(
                    Tree(
                        'entry',
                        [
                            token('-', 'MINUS', entry.source),
                            token(entry.ordering, 'INT', entry.source),
                            token(entry.code, 'NAME', entry.source),
                            token(':', 'COLON', entry.source),
                            scalar(Value('string', entry.decode, ''), entry.source),
                        ],
                    )
                )
        namespace: Tree[Token] = Tree(
            'namespace_decl',
            [
                token('namespace', 'NAMESPACE', source),
                Tree('qualified_name', [token(source.namespace, 'NAME', source)]),
            ],
        )
        return Document(
            Path(source.path),
            '',
            Tree('start', [namespace, Tree(kind, body)]),
            builtin=source.origin == 'builtin',
            repository='' if source.origin == 'builtin' else 'model',
        )


def restore(model: Model) -> Analysis:
    """Check embedded declarations using their embedded definitions exclusively.

    :param model: Shape-checked intermediate model.
    :returns: Shared semantic analysis.
    :raises ValueError: If a resolved identity is missing or inconsistent.
    """
    documents: list[Document] = []
    bindings: list[dict[str, EntityKey]] = []
    for definition in model.definitions:
        if definition.derived:
            continue
        groups: dict[tuple[str, str], tuple[list[Field], list[Dimension]]] = {}
        own_dimensions: list[Dimension] = []
        for dimension in definition.dimensions:
            source, owner = dimension.source, definition.source
            if (
                source.path == owner.path
                and source.namespace == owner.namespace
                and (owner.line, owner.column) <= (source.line, source.column)
                and (source.end_line, source.end_column) <= (owner.end_line, owner.end_column)
            ):
                own_dimensions.append(dimension)
            else:
                groups.setdefault((source.path, source.namespace), ([], []))[1].append(dimension)
        for field in definition.fields:
            if field.extension:
                groups.setdefault((field.source.path, field.source.namespace), ([], []))[0].append(
                    field
                )
        adapter = Restoring()
        documents.append(adapter.declaration(definition, dimensions=tuple(own_dimensions)))
        bindings.append(adapter.bindings)
        for fields, dimensions in groups.values():
            adapter = Restoring()
            documents.append(adapter.declaration(definition, tuple(fields), tuple(dimensions)))
            bindings.append(adapter.bindings)
    diagnostics: list[Diagnostic] = []
    units = resolve_scopes(
        documents,
        diagnostics,
        [d for d in documents if d.builtin],
        {c.name: c.value for c in model.constants},
    )
    symbols = {symbol.key: symbol for unit in units for symbol in unit.scope.values()}
    for unit, references in zip(units, bindings, strict=True):
        for name, key in references.items():
            if key not in symbols:
                raise ValueError(
                    f"$.definitions: missing reference {key[0]}.{key[1]} ({unit.document.path})"
                )
            unit.scope[name] = symbols[key]
    return analyze_units(units, diagnostics)
