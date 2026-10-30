"""Lower checked declarations to source-independent semantic records."""

import json
from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from lark import Token, Tree

from yambl_compiler.ir.codec import encode, seal, semantic
from yambl_compiler.ir.expansion import expand
from yambl_compiler.ir.model import (
    Access,
    Argument,
    Bound,
    Calculation,
    CalculationAccess,
    CalculationInput,
    Call,
    Definition,
    Dimension,
    Effective,
    Entry,
    Expression,
    External,
    Field,
    Formal,
    HierarchyEntry,
    LogicalField,
    Lookup,
    LookupBinding,
    LookupKey,
    LookupStep,
    Model,
    Operation,
    Source,
    Text,
    Type,
    UniqueKey,
    Value,
)
from yambl_compiler.languages.yambl.computation.records import Expression as CalculationExpression
from yambl_compiler.languages.yambl.computation.records import TypeHint
from yambl_compiler.languages.yambl.semantics.decodes import is_flags
from yambl_compiler.languages.yambl.semantics.decorators import bind_call, parameters, unique_fields
from yambl_compiler.languages.yambl.semantics.dimensions import (
    Dimension as CheckedDimension,
)
from yambl_compiler.languages.yambl.semantics.dimensions import (
    dimension_at,
    field_dimension,
    resolve_dimension,
)
from yambl_compiler.languages.yambl.semantics.expressions import (
    REGEX_REFERENCE,
    Resolver,
    Term,
    integer_text,
)
from yambl_compiler.languages.yambl.semantics.fields import field_name
from yambl_compiler.languages.yambl.semantics.hierarchies import hierarchy_entries
from yambl_compiler.languages.yambl.semantics.symbols import (
    GENERICS,
    VALUES,
    EntityKey,
    Unit,
    header,
    namespace,
    qualified,
)
from yambl_compiler.languages.yambl.semantics.types import (
    Resolved,
    common_type,
    portable_finite_value,
    portable_flags,
)
from yambl_compiler.languages.yambl.semantics.validation import Analysis
from yambl_compiler.languages.yambl.syntax.computation import branches
from yambl_compiler.languages.yambl.syntax.computation import expression as calculation_expression
from yambl_compiler.languages.yambl.syntax.parser import (
    annotations,
    children,
    field_mode,
    string_value,
    tokens,
)


def literal(token: Token) -> Value:
    """Normalize one scalar without binary floating-point conversion.

    :param token: Checked scalar token.
    :returns: Tagged exact scalar.
    """
    if token.type == 'STRING':
        return Value('string', string_value(token), '')
    if token.type == 'BOOLEAN':
        return Value('bool', str(token), '')
    if token.type == 'INT':
        return Value('integer', integer_text(str(token)), '')
    return Value('decimal', str(Decimal(str(token))), '')


class Lowering:
    """Convert analyzed scopes while retaining original declaration contexts."""

    def __init__(self, analysis: Analysis, root: Path, prefix: str, version: str) -> None:
        """Retain analysis and portable artifact provenance.

        :param analysis: Successful semantic analysis.
        :param root: Original root, or a relative placeholder for embedded models.
        :param prefix: Repository prefix.
        :param version: Producer version.
        :returns: None.
        """
        self.analysis = analysis
        self.root = root
        self.prefix = prefix
        self.version = version
        self.world = analysis.units[0].world
        self.external: dict[str, list[Source]] = defaultdict(list)
        self.resolved_fields: dict[str, Resolved] = {}
        self.owners = {
            id(site.declaration): (key, name)
            for key, members in analysis.entities.fields.items()
            for name, site in members.items()
        }
        self.extensions = {
            id(field)
            for unit in analysis.units
            for extension in children(unit.document.tree, 'extension')
            for field in children(extension, 'field')
        }
        for key, (_, declaration) in self.world.declarations.items():
            for field in children(declaration, 'field'):
                self.owners.setdefault(
                    id(field), (key, field_name(children(field, 'field_body')[0]))
                )

    def source(self, unit: Unit, node: Tree[Token]) -> Source:
        """Translate token positions to a portable source span.

        :param unit: Original lexical unit.
        :param node: Located declaration or expression.
        :returns: Relative source location.
        """
        path = unit.document.path
        if path.is_absolute():
            if path.is_relative_to(self.root):
                path = path.relative_to(self.root)
            else:
                # Installed resources live beneath the compiler's lang directory.
                path = Path(*path.parts[path.parts.index('lang') + 1 :])
        first, last = tokens(node)[0], tokens(node)[-1]
        return Source(
            'builtin' if unit.document.builtin else 'model',
            path.as_posix(),
            namespace(unit.document),
            first.line or 1,
            first.column or 1,
            last.end_line or 1,
            last.end_column or 1,
        )

    def identity(self, key: EntityKey, name: str) -> str:
        """Look up an already checked logical field identity.

        :param key: Declaring owner.
        :param name: Local field name.
        :returns: Qualified logical identity.
        """
        owner, field = self.analysis.entities.identities.get((key, name), (key, name))
        return qualified(owner) + '.' + field

    def type(self, resolved: Resolved) -> Type:
        """Convert a checked contract without exporting internal repr strings.

        :param resolved: Checked type.
        :returns: Portable type contract.
        """
        exact = None
        if resolved.exact_value is not None:
            exact = portable_finite_value(resolved.exact_value)
        elif resolved.exact is not None:
            value = resolved.exact
            target = qualified(resolved.key) if resolved.key else ''
            if resolved.exact_key is not None:
                target = qualified(resolved.exact_key)
                exact = Value('type' if resolved.name == 'type' else 'dictionary', '', target)
            elif resolved.name == 'code':
                key = resolved.args[0].key
                target = qualified(key) if key else ''
                declaration = self.world.declarations.get(key or ('', ''))
                if declaration and is_flags(declaration[1]):
                    exact = portable_flags(resolved, self.world)
                else:
                    exact = Value('code', value, target)
            else:
                kind = (
                    'reference'
                    if resolved.kind == 'reference'
                    else (
                        'bool'
                        if resolved.name == 'bool'
                        else (
                            'integer'
                            if resolved.name in ('integer', 'snowflake_id')
                            else ('decimal' if resolved.name in ('decimal', 'float') else 'string')
                        )
                    )
                )
                text = (
                    integer_text(value)
                    if kind == 'integer'
                    else str(Decimal(value)) if kind == 'decimal' else value
                )
                exact = Value(cast('LiteralValueKind', kind), text, target)
        candidates: set[str] = set()
        if resolved.name == 'attrs':
            category, target_type = resolved.args
            modes = (
                ('static', 'config', 'snapshot', 'scd')
                if category.exact is None
                else tuple(c.lower() for c in category.exact.split('+'))
            )
            for mode in modes:
                for sites in self.analysis.entities.candidates(
                    target_type.key or ('', ''), mode
                ).values():
                    for site in sites:
                        owner, name = self.owners[id(site.declaration)]
                        candidates.add(self.identity(owner, name))
        return Type(
            resolved.name,
            resolved.kind,
            qualified(resolved.key) if resolved.key else '',
            (
                tuple(
                    sorted(
                        (self.type(arg) for arg in resolved.args),
                        key=lambda t: json.dumps(encode(t), sort_keys=True),
                    )
                )
                if resolved.name == 'dependent'
                else tuple(self.type(arg) for arg in resolved.args)
            ),
            tuple(Text(*p) for p in resolved.params),
            exact,
            bool(resolved.dependency),
            tuple(
                sorted(
                    (self.type(member) for member in resolved.members),
                    key=lambda t: json.dumps(encode(t), sort_keys=True),
                )
            ),
            tuple(sorted(candidates)),
            tuple(
                sorted(
                    (portable_finite_value(value) for value in resolved.allowed_values),
                    key=lambda value: json.dumps(encode(value), sort_keys=True, ensure_ascii=False),
                )
            ),
        )

    def expression(
        self,
        node: Tree[Token],
        unit: Unit,
        resolver: Resolver,
        bindings: set[str] | None = None,
        dictionary: bool = False,
    ) -> Expression:
        """Resolve lexical roots while preserving explicit dependent operations.

        :param node: Checked expression tree.
        :param unit: Original lexical scope.
        :param resolver: Instance and parameter context.
        :param bindings: Lexical parameter names.
        :param dictionary: Whether this is a code dictionary operand.
        :returns: Structured expression with resolved identities.
        """
        bindings = set(bindings or ())
        finite = children(node, 'one_of_type') or children(node, 'exact_type')
        if finite:
            name = 'exact' if finite[0].data == 'exact_type' else 'one_of'
            candidates = tuple(
                calculation_expression(candidate) for candidate in branches(finite[0])
            )
            finite_refs: dict[str, str] = {}

            def collect(expression: CalculationExpression) -> None:
                """Record lexical identities referenced by a finite candidate.

                :param expression: Candidate subtree.
                :returns: None; records visible declaration bindings.
                """
                symbol = unit.scope.get(expression.value) if expression.kind == 'name' else None
                if symbol and symbol.kind in ('constant', 'decodes', 'entity'):
                    finite_refs[expression.value] = qualified(symbol.key)
                for child in expression.operands:
                    collect(child)
                for argument in expression.arguments:
                    collect(argument.expression)
                for case in expression.cases:
                    collect(case.expression)

            for candidate in candidates:
                collect(candidate)
            resolved = resolver.resolve(node)
            values = tuple(
                sorted(
                    (portable_finite_value(value) for value in resolved.allowed_values),
                    key=lambda value: json.dumps(encode(value), sort_keys=True, ensure_ascii=False),
                )
            )
            if name == 'exact':
                exact = self.type(resolved).exact
                values = (exact,) if exact is not None else ()
            return Expression(
                'finite',
                name,
                '',
                None,
                (),
                (),
                (),
                (),
                '',
                self.source(unit, node),
                tuple(Text(name, target) for name, target in sorted(finite_refs.items())),
                candidates,
                values,
            )
        first = tokens(node)[0]
        name = str(first)
        target = ''
        kind: ExpressionKind
        value = None
        symbol = unit.scope.get(string_value(first) if first.type == 'STRING' else name)
        if dictionary and first.type == 'STRING':
            if symbol:
                kind, name, target = 'reference', symbol.key[1], qualified(symbol.key)
            else:
                kind, name = 'external', string_value(first)
                from yambl_compiler.languages.yambl.semantics.symbols import symbol_key

                target = qualified(symbol_key(unit.document, name))
                self.external[target].append(self.source(unit, node))
        elif first.type in ('STRING', 'INT', 'NUMBER', 'BOOLEAN'):
            kind, value, name = 'literal', literal(first), ''
        elif name in bindings:
            kind = 'parameter'
        elif name == 'self':
            kind = 'self'
        elif name in GENERICS:
            kind = 'generic'
        elif symbol:
            kind, target = 'reference', qualified(symbol.key)
        else:
            kind = 'primitive'
        groups = children(node, 'arguments')
        args = children(groups[0], 'type_expr') if groups else []
        lowered: list[Expression] = []
        for i, arg in enumerate(args):
            lowered.append(
                self.expression(arg, unit, resolver, bindings, name == 'code' and i == 0)
            )
            bind = children(arg, 'binding')
            if bind:
                bindings.add(str(tokens(bind[0])[-1]))
        accesses: list[Access] = []
        prefix: list[Tree[Token] | Token] = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == 'access':
                operator, member = map(str, tokens(child))
                selected = ''
                if operator == '.' and name not in bindings:
                    term = resolver.evaluate(Tree('type_expr', list(prefix)))
                    scope_key = term.type.key
                    if term.path == 'self':
                        source = resolver.self_fields.get(member)
                        if source:
                            from yambl_compiler.languages.yambl.semantics.symbols import field_owner

                            owner = field_owner(*source)
                            scope_key = owner.key if owner else None
                    if scope_key and member in self.world.fields.get(scope_key, {}):
                        selected = self.identity(scope_key, member)
                accesses.append(
                    Access(
                        cast('AccessOperator', operator),
                        member,
                        selected,
                        self.source(unit, child),
                    )
                )
            prefix.append(child)
        params = children(node, 'parameters')
        normalized = resolver.parameters(node, name, expand_regex=False) if params else ()
        references: dict[str, str] = {}
        if name == 'string':
            for _, pattern in normalized:
                for match in REGEX_REFERENCE.finditer(pattern):
                    slashes, local = match.groups()
                    if not len(slashes) % 2:
                        references[local] = qualified(unit.scope[local].key)
        bind = children(node, 'binding')
        result = Expression(
            kind,
            name,
            target,
            value,
            tuple(lowered),
            tuple(Text(*p) for p in normalized),
            tuple(accesses),
            tuple(
                Operation(
                    cast('FlagOperator', str(tokens(op)[0])),
                    self.expression(children(op, 'flag_operand')[0], unit, resolver, bindings),
                )
                for op in children(node, 'flag_operation')
            ),
            str(tokens(bind[0])[-1]) if bind else '',
            self.source(unit, node),
            tuple(Text(local, target) for local, target in sorted(references.items())),
        )
        if (
            symbol
            and symbol.kind == 'decodes'
            and accesses
            and is_flags(self.world.declarations[symbol.key][1])
        ):
            evaluated = resolver.evaluate(node).type
            if evaluated.exact is not None:
                selected_codes = evaluated.exact.split('+') if evaluated.exact else ['None']
                result = replace(
                    result,
                    accesses=(replace(accesses[0], name=selected_codes[0]),),
                    operations=(),
                )
                result = replace(
                    result,
                    operations=tuple(
                        Operation(
                            '+',
                            replace(
                                result,
                                accesses=(replace(accesses[0], name=code),),
                                binding='',
                            ),
                        )
                        for code in selected_codes[1:]
                    ),
                )
        return result

    def calls(self, node: Tree[Token], unit: Unit, resolver: Resolver) -> tuple[Call, ...]:
        """Bind metadata once and retain each occurrence.

        :param node: Decorated declaration or field.
        :param unit: Original lexical scope.
        :param resolver: Field context for expression selection.
        :returns: Canonically ordered metadata applications.
        """
        result: list[Call] = []
        for call in annotations(node):
            bound: list[tuple[str, str, Resolved]] = []
            bind_call(call, node, unit, [], bound_arguments=bound)
            arguments: list[Argument] = []
            for arg in [c for c in call.children if isinstance(c, Tree)]:
                kind = str(arg.data).removesuffix('_argument')
                arguments.append(
                    Argument(
                        cast('ArgumentKind', kind),
                        '' if kind == 'positional' else str(tokens(arg)[0]),
                        self.expression(children(arg, 'type_expr')[0], unit, resolver),
                        (
                            self.expression(
                                children(children(arg, 'decorator_default')[0], 'type_expr')[0],
                                unit,
                                resolver,
                            )
                            if children(arg, 'decorator_default')
                            else None
                        ),
                    )
                )
            target = qualified(unit.scope[str(tokens(call)[1])].key)
            dimension = (
                field_dimension(unit, node, call, [])
                if target == 'builtins.dimension' and node.data == 'field'
                else None
            )
            result.append(
                Call(
                    target,
                    tuple(arguments),
                    tuple(Bound(n, k, self.type(t)) for n, k, t in bound),
                    self.source(unit, call),
                    qualified(dimension.identity) if dimension else '',
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda call: json.dumps(semantic(encode(call)), sort_keys=True),
            )
        )

    def calculation(self, key: EntityKey, name: str, unit: Unit) -> Calculation | None:
        """Lower inferred computation contracts and lexical references.

        :param key: Declaring entity identity.
        :param name: Local field name.
        :param unit: Original lexical scope.
        :returns: Portable calculation, absent for stored fields.
        """
        checked = self.analysis.calculations.get((key, name))
        if checked is None:
            return None
        references: dict[str, str] = {}

        def visit(expression: CalculationExpression) -> None:
            """Collect resolved lexical constants without retaining unused imports.

            :param expression: Calculation subtree.
            :returns: None.
            """
            if expression.kind == "name":
                symbol = unit.scope.get(expression.value)
                if symbol is not None:
                    references[expression.value] = qualified(symbol.key)
            for operand in expression.operands:
                visit(operand)
            for argument in expression.arguments:
                visit(argument.expression)
            for case in expression.cases:
                visit(case.expression)

        for binding in checked.body.bindings:
            visit(binding.expression)
            if binding.annotation:

                def visit_hint(hint: TypeHint) -> None:
                    """Collect declaration references from a local type annotation.

                    :param hint: Type annotation subtree.
                    :returns: None; records its lexical declarations.
                    """
                    symbol = unit.scope.get(hint.name)
                    if symbol is not None:
                        references[hint.name] = qualified(symbol.key)
                    for candidate in hint.finite_candidates:
                        visit(candidate)
                    for argument in hint.arguments:
                        visit_hint(argument)
                    for operation in hint.operations:
                        visit_hint(operation.operand)

                visit_hint(binding.annotation)
        visit(checked.body.result)
        return Calculation(
            checked.body,
            tuple(
                CalculationInput(
                    p.name,
                    self.type(p.type),
                    p.required,
                    p.default,
                    tuple(
                        replace(
                            self.source(scope, scope.document.tree),
                            line=site.line,
                            column=site.column,
                            end_line=site.end_line,
                            end_column=site.end_column,
                        )
                        for scope, site in p.sites
                    ),
                    qualified(p.default_dimension) if p.default_dimension else '',
                )
                for p in checked.inputs
            ),
            tuple(
                CalculationAccess(
                    a.span,
                    self.identity(a.owner, a.name),
                    qualified(a.owner) + "." + a.name,
                    cast("CalculationOperation", a.operation),
                )
                for a in checked.accesses
            ),
            tuple(qualified(owner) + "." + local for owner, local in checked.dependencies),
            tuple(Text(n, t) for n, t in sorted(references.items())),
        )

    def lookups(self, key: EntityKey) -> tuple[Lookup, ...]:
        """Lower rules declared by an entity, retaining resolved paths and scopes.

        :param key: Declaring entity identity.
        :returns: Portable rules in deterministic name order.
        """
        result: list[Lookup] = []
        for name, rule in sorted(self.analysis.lookups.get(key, {}).items()):
            if rule.owner != key:
                continue
            references: dict[str, str] = {}

            def visit(expression: CalculationExpression) -> None:
                """Record lexical declarations referenced by an expression.

                :param expression: Path or binding expression.
                :returns: None.
                """
                if expression.kind == 'name':
                    symbol = rule.unit.scope.get(expression.value)
                    if symbol is not None:
                        references[expression.value] = qualified(symbol.key)
                for operand in expression.operands:
                    visit(operand)
                for argument in expression.arguments:
                    visit(argument.expression)

            keys: list[LookupKey] = []
            for checked, syntax in zip(rule.keys, children(rule.node, 'lookup_key'), strict=True):
                visit(checked.expression)
                steps: list[LookupStep] = []
                for step in checked.steps:
                    bindings: list[LookupBinding] = []
                    for parameter in sorted(
                        step.parameters, key=lambda item: item.requirement.name
                    ):
                        value = parameter.value
                        kind: LookupFixedKind = ''
                        text = target = ''
                        if not parameter.input_name and value is not None:
                            if parameter.requirement.type.name == 'code':
                                kind = 'code'
                                dictionary = parameter.requirement.type.args[0].key
                                assert dictionary is not None
                                target = qualified(dictionary)
                                text = str(value)
                            elif type(value) is date:
                                kind, text = 'date', value.isoformat()
                            else:
                                assert isinstance(value, str)
                                kind, text = 'string', value
                        bindings.append(
                            LookupBinding(
                                parameter.requirement.name,
                                self.type(parameter.requirement.type),
                                parameter.input_name,
                                kind,
                                text,
                                target,
                                parameter.binding,
                            )
                        )
                    access = step.access
                    steps.append(
                        LookupStep(
                            CalculationAccess(
                                access.span,
                                self.identity(access.owner, access.name),
                                qualified(access.owner) + '.' + access.name,
                                'value',
                            ),
                            tuple(bindings),
                        )
                    )
                keys.append(
                    LookupKey(
                        name=checked.name,
                        type=self.type(checked.type),
                        expression=checked.expression,
                        steps=tuple(steps),
                        source=self.source(rule.unit, syntax),
                    )
                )
            inputs = tuple(
                CalculationInput(
                    p.name,
                    self.type(p.type),
                    p.required,
                    p.default,
                    tuple(
                        replace(
                            self.source(scope, scope.document.tree),
                            line=span.line,
                            column=span.column,
                            end_line=span.end_line,
                            end_column=span.end_column,
                        )
                        for scope, span in p.sites
                    ),
                    qualified(p.default_dimension) if p.default_dimension else '',
                )
                for p in rule.inputs
            )
            result.append(
                Lookup(
                    name,
                    tuple(keys),
                    inputs,
                    tuple(Text(n, t) for n, t in sorted(references.items())),
                    rule.declaration.data == 'extension',
                    self.source(rule.unit, rule.node),
                    self.calls(
                        rule.node,
                        rule.unit,
                        Resolver(rule.unit, [], self.analysis.entities.field_types(key), True),
                    ),
                )
            )
        return tuple(result)

    def dimensions(self, key: EntityKey, unit: Unit) -> tuple[Dimension, ...]:
        """Lower entity-local dimension definitions, including extensions.

        :param key: Entity identity.
        :param unit: Declaration context.
        :returns: Sorted portable dimension declarations.
        """
        result: list[Dimension] = []
        for name in sorted(self.world.dimensions.get(key, {})):
            dimension = resolve_dimension(unit, key, name, [])
            assert dimension is not None
            result.append(self.dimension(dimension))
        return tuple(result)

    def dimension(self, dimension: CheckedDimension) -> Dimension:
        """Lower one dimension in its declaration scope.

        :param dimension: Checked local or shared declaration.
        :returns: Portable identity, type and lexical selection expressions.
        """
        syntax = children(children(dimension.node, 'dimension_value')[0], 'type_expr')[0]
        resolver = Resolver(dimension.unit, [], self.analysis.entities.field_types(dimension.owner))
        references: dict[str, str] = {}

        def visit(expression: CalculationExpression) -> None:
            """Retain declaration names used by selection expressions.

            :param expression: Expression subtree.
            :returns: None; records resolved names.
            """
            if expression.kind == 'name':
                symbol = dimension.unit.scope.get(expression.value)
                if symbol is not None:
                    references[expression.value] = qualified(symbol.key)
            for child in expression.operands:
                visit(child)
            for argument in expression.arguments:
                visit(argument.expression)
            for case in expression.cases:
                visit(case.expression)

        for expression in (dimension.hierarchy_default, dimension.fallback):
            if expression:
                visit(expression)
        return Dimension(
            dimension.name,
            dimension.value_name,
            self.expression(syntax, dimension.unit, resolver),
            self.type(dimension.value_type),
            dimension.hierarchy_name,
            dimension.hierarchy_default,
            dimension.fallback,
            self.source(dimension.unit, dimension.node),
            qualified(dimension.identity),
            tuple(Text(name, target) for name, target in sorted(references.items())),
        )

    def definition(self, key: EntityKey, unit: Unit, node: Tree[Token]) -> Definition:
        """Lower a declaration and all merged implementations it owns.

        :param key: Declared identity.
        :param unit: Original scope.
        :param node: Declaration syntax.
        :returns: Complete semantic definition.
        """
        context = self.analysis.entities.field_types(key)
        resolver = Resolver(unit, [], context, True)
        formals: list[Formal] = []
        bindings: set[str] = set()
        if node.data == 'decorator':
            for parameter in parameters(node):
                formals.append(
                    Formal(
                        parameter.name,
                        self.expression(parameter.type, unit, resolver),
                        (
                            self.expression(parameter.default, unit, resolver)
                            if parameter.default
                            else None
                        ),
                        parameter.pair,
                        parameter.optional,
                        self.source(unit, parameter.node),
                        parameter.variadic,
                    )
                )
        else:
            for group in children(header(node), 'type_parameters'):
                for p in children(group, 'type_parameter'):
                    name = str(tokens(p)[0])
                    syntax = children(p, 'type_expr')[0]
                    formals.append(
                        Formal(
                            name,
                            self.expression(syntax, unit, resolver, bindings),
                            None,
                            False,
                            False,
                            self.source(unit, p),
                        )
                    )
                    bindings.add(name)
                    resolver.bindings[name] = Term(resolver.resolve(syntax), 'value', '$' + name)
        fields: list[Field] = []
        local = self.analysis.entities.fields.get(key, {})
        sites = [(name, site.unit, site.declaration, site.builtin) for name, site in local.items()]
        if node.data == 'value' or (
            node.data == 'entity' and key not in self.analysis.entities.fields
        ):
            sites = [
                (field_name(children(f, 'field_body')[0]), unit, f, False)
                for f in children(node, 'field')
            ]
        for name, scope, field, builtin in sorted(sites):
            body = children(field, 'field_body')[0]
            syntax = children(body, 'type_expr')[0]
            field_resolver = Resolver(scope, [], context, True)
            resolved = field_resolver.resolve(syntax)
            self.resolved_fields[qualified(key) + '.' + name] = resolved
            defaults = children(body, 'default_value')
            mode = field_mode(body)
            if node.data == 'value':
                mode = 'static'
            fields.append(
                Field(
                    name,
                    self.identity(key, name),
                    qualified(key) + '.' + name,
                    cast('FieldCategory', mode),
                    self.expression(syntax, scope, field_resolver),
                    self.expression(syntax, scope, field_resolver),
                    self.type(resolved),
                    literal(tokens(defaults[0])[1]) if defaults else None,
                    self.calls(field, scope, field_resolver),
                    id(field) in self.extensions,
                    self.source(scope, body),
                    self.calculation(key, name, scope),
                    builtin,
                    tokens(body)[0].type == 'ABSTRACT',
                )
            )
        effective: list[Effective] = []
        for name, site in sorted(self.analysis.entities.effective.get(key, {}).items()):
            owner, local_name = self.owners[id(site.declaration)]
            effective.append(
                Effective(
                    name,
                    qualified(owner) + '.' + local_name,
                    self.identity(owner, local_name),
                )
            )
        entries: list[Entry] = []
        for entry in children(node, 'entry'):
            parts = tokens(entry)
            order = str(int(str(parts[1]), 2)) if '0b' in parts[1] else integer_text(str(parts[1]))
            entries.append(
                Entry(
                    str(parts[2]),
                    order,
                    string_value(parts[4]),
                    False,
                    self.source(unit, entry),
                )
            )
        if node.data == 'decodes' and is_flags(node):
            entries.extend(
                [
                    Entry('None', '0', 'None', True, self.source(unit, header(node))),
                    Entry(
                        'All',
                        str(sum(int(e.ordering) for e in entries)),
                        'All',
                        True,
                        self.source(unit, header(node)),
                    ),
                ]
            )
        parent = children(header(node), 'inheritance')
        parent_name = str(tokens(parent[0])[1]) if parent else ''
        parent_key = (
            'Entity'
            if parent_name == 'Entity'
            else qualified(unit.scope[parent_name].key) if parent_name else ''
        )
        return Definition(
            cast('DefinitionKind', str(node.data)),
            qualified(key).removeprefix('model::'),
            qualified(key),
            node.data == 'value' or str(tokens(header(node))[0]) == 'final',
            parent_key,
            tuple(formals),
            (
                self.expression(children(node, 'type_expr')[0], unit, resolver, bindings)
                if node.data in ('type_alias', 'constant')
                else None
            ),
            self.calls(node, unit, resolver),
            tuple(fields),
            tuple(effective),
            tuple(sorted(entries, key=lambda entry: entry.code)),
            self.source(unit, header(node)),
            (
                (self.dimension(dimension_at(unit, node, key, key, [])),)
                if node.data == 'dimension'
                else self.dimensions(key, unit)
            ),
            unit.world.constant_expressions.get(key) if node.data == 'constant' else None,
            (
                tuple(
                    Text(n, qualified(s.key))
                    for n, s in sorted(unit.scope.items())
                    if s.kind in ('constant', 'decodes', 'entity')
                )
                if node.data == 'constant'
                else ()
            ),
            lookups=self.lookups(key),
            effective_lookups=tuple(
                Text(name, qualified(rule.owner))
                for name, rule in sorted(self.analysis.lookups.get(key, {}).items())
            ),
            hierarchy_entries=(
                tuple(
                    HierarchyEntry(
                        str(entry.ordering),
                        entry.value,
                        qualified(entry.dictionary) if entry.dictionary else '',
                        self.source(unit, entry.node),
                    )
                    for entry in hierarchy_entries(unit, node, [])
                )
                if node.data == 'hierarchy'
                else ()
            ),
            abstract=node.data == 'entity' and tokens(header(node))[0].type == 'ABSTRACT',
            unique_keys=tuple(
                UniqueKey(
                    qualified(key), unique_fields(call, node, unit, []), self.source(unit, call)
                )
                for call in annotations(node, 'unique_by')
                if node.data == 'entity'
                and (symbol := unit.scope.get('unique_by')) is not None
                and symbol.key[0].split('\0')[-1] == 'builtins'
            ),
        )

    def model(self) -> Model:
        """Produce a deterministic complete artifact.

        :returns: Sealed model containing user and embedded definitions.
        """
        declarations = {
            qualified(key): (key, unit, node)
            for key, (unit, node) in self.world.declarations.items()
        }
        definitions = tuple(self.definition(*declarations[name]) for name in sorted(declarations))
        by_identity = {definition.identity: definition for definition in definitions}
        derived: list[Definition] = []
        for key, entries in self.world.subtypes.items():
            owner_key = (key[0], key[1].removesuffix('::subtypes'))
            owner = by_identity[qualified(owner_key)]
            derived.append(
                Definition(
                    kind='decodes',
                    name=owner.name + '::subtypes',
                    identity=owner.identity + '::subtypes',
                    final=False,
                    parent='',
                    parameters=(),
                    expression=None,
                    decorators=(),
                    fields=(),
                    effective=(),
                    entries=tuple(
                        Entry(
                            entry.code,
                            str(entry.ordering),
                            entry.decode,
                            True,
                            by_identity[qualified(entry.target)].source,
                        )
                        for entry in entries
                    ),
                    source=owner.source,
                    derived=True,
                )
            )
        definitions = tuple(sorted((*definitions, *derived), key=lambda d: d.identity))
        aliases = {d.identity: d for d in definitions if d.kind == 'type_alias'}
        definitions = tuple(
            replace(
                d,
                fields=tuple(replace(f, expanded=expand(f.expression, aliases)) for f in d.fields),
            )
            for d in definitions
        )
        groups: dict[str, list[Field]] = defaultdict(list)
        applicable: dict[str, set[str]] = defaultdict(set)
        for definition in definitions:
            for field in definition.fields:
                groups[field.identity].append(field)
                if definition.kind == 'value':
                    applicable[field.identity].add(definition.identity)
            for effective in definition.effective:
                applicable[effective.identity].add(definition.identity)
        contracts = {
            qualified(key) + '.' + name: contract
            for (key, name), contract in self.analysis.entities.contracts.items()
        }
        logical_fields = tuple(
            LogicalField(
                identity,
                self.type(
                    contracts[identity]
                    if identity in contracts
                    else cast(
                        Resolved,
                        common_type(
                            [self.resolved_fields[f.implementation] for f in group],
                            self.world,
                        ),
                    )
                ),
                tuple(sorted(f.implementation for f in group)),
                tuple(sorted(applicable[identity])),
                next((f.calculation.inputs for f in group if f.calculation is not None), ()),
            )
            for identity, group in sorted(groups.items())
        )
        primitives = tuple(
            Text(
                name,
                (
                    'operator'
                    if name in GENERICS
                    else 'abstract_entity' if name == 'Entity' else 'value'
                ),
            )
            for name in sorted(VALUES | GENERICS | {'Entity'})
        )
        return seal(
            Model(
                13,
                self.version,
                self.prefix,
                primitives,
                tuple(Text(k, v) for k, v in sorted(self.world.constants.items())),
                definitions,
                logical_fields,
                tuple(
                    External(
                        k,
                        tuple(
                            sorted(
                                set(v),
                                key=lambda s: (s.origin, s.path, s.line, s.column),
                            )
                        ),
                    )
                    for k, v in sorted(self.external.items())
                ),
                '',
                '',
            )
        )


# Static aliases keep constructors precise without duplicating runtime dispatch tables.
type LookupFixedKind = Literal['', 'string', 'date', 'code']
type LiteralValueKind = Literal[
    'string',
    'integer',
    'decimal',
    'bool',
    'reference',
    'type',
    'dictionary',
    'code',
    'flags',
]
type AccessOperator = Literal['.', '::']
type FlagOperator = Literal['+', '-']
type ExpressionKind = Literal[
    'primitive', 'generic', 'reference', 'parameter', 'self', 'literal', 'external'
]
type ArgumentKind = Literal['positional', 'keyword', 'pair']
type FieldCategory = Literal['id', 'static', 'config', 'snapshot', 'scd', "computed"]
type CalculationOperation = Literal["best_available", "value"]
type DefinitionKind = Literal[
    'entity', 'value', 'type_alias', 'decorator', 'decodes', 'constant', 'dimension', 'hierarchy'
]
