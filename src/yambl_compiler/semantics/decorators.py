"""Bind decorator arguments and validate declarative metadata."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from lark import Token, Tree

from yambl_compiler.semantics.expressions import Resolver, Term
from yambl_compiler.semantics.symbols import Unit, header
from yambl_compiler.semantics.types import Resolved, accepts
from yambl_compiler.syntax.parser import Diagnostic, annotations, children, diagnostic, tokens


@dataclass(frozen=True)
class Parameter:
    """Retain one declared decorator parameter.

    :param node: Original syntax and location.
    :param name: Formal parameter name.
    :param type: Value constraint or pair type constraint.
    :param pair: Whether this parameter receives a new key and type.
    :param optional: Whether a pair can be omitted.
    :param default: Ordinary parameter's default expression, if present.
    """

    node: Tree[Token]
    name: str
    type: Tree[Token]
    pair: bool
    optional: bool
    default: Tree[Token] | None


def parameters(declaration: Tree[Token]) -> list[Parameter]:
    """Read formals without interpreting their constraints.

    :param declaration: Decorator declaration.
    :returns: Parameters in declaration order.
    """
    result: list[Parameter] = []
    for node in children(header(declaration), 'decorator_parameter'):
        items = tokens(node)
        defaults = children(node, 'decorator_default')
        result.append(
            Parameter(
                node,
                str(next(t for t in items if t.type == 'NAME')),
                children(node, 'type_expr')[0],
                str(items[0]) == '*',
                any(str(t) == '?' for t in items),
                children(defaults[0], 'type_expr')[0] if defaults else None,
            )
        )
    return result


def capture(node: Tree[Token], unit: Unit) -> Term:
    """Capture a decorated definition without inventing an instance.

    :param node: Decorated declaration or field.
    :param unit: Definition scope.
    :returns: The definition's expression category.
    """
    if node.data == 'field':
        syntax = children(children(node, 'field_body')[0], 'type_expr')[0]
        return Term(Resolved('attrs'), 'field', 'self', (unit, syntax))
    name = next(t for t in tokens(header(node)) if t.type == 'NAME')
    if node.data == 'decorator':
        return Term(Resolved('decorator', key=unit.scope[str(name)].key), 'value')
    return Resolver(unit, [], {}).evaluate(Tree('type_expr', [name]))


def argument_type(resolver: Resolver, syntax: Tree[Token], expected: Resolved) -> Resolved:
    """Check a value, including a contextually typed reference ID.

    :param resolver: Lexical and captured-object context.
    :param syntax: Argument or default expression.
    :param expected: Declared value constraint.
    :returns: Actual type, preserving a constant value when available.
    """
    actual = resolver.value_type(resolver.evaluate(syntax))
    literal_types = {
        'string': {'STRING'},
        'integer': {'INT'},
        'snowflake_id': {'INT'},
        'float': {'INT', 'NUMBER'},
        'decimal': {'INT', 'NUMBER'},
        'bool': {'BOOLEAN'},
    }
    items = tokens(syntax)
    if len(items) == 1 and items[0].type in literal_types.get(expected.name, set()):
        actual = replace(expected, exact=actual.exact)
    if expected.kind == 'reference' and actual.name == 'string':
        fields = resolver.unit.world.fields.get(expected.key or ('', ''), {})
        ids = [
            (scope, field)
            for scope, field in fields.values()
            if str(tokens(children(field, 'field_body')[0])[0]) == 'id'
        ]
        if len(ids) != 1:
            resolver.issue(syntax, 'reference argument requires one identifiable ID field')
        else:
            scope, field = ids[0]
            id_type = Resolver(scope, resolver.diagnostics, {}).resolve(
                children(children(field, 'field_body')[0], 'type_expr')[0]
            )
            if id_type.name in ('string', 'snowflake_id'):
                actual = replace(expected, exact=actual.exact)
    if actual.kind == 'unknown' or not accepts(expected, actual, resolver.unit.world):
        resolver.issue(syntax, 'incompatible decorator argument')
    return actual


def check_parameters(declaration: Tree[Token], unit: Unit, diagnostics: list[Diagnostic]) -> None:
    """Validate even unused decorator signatures and independent defaults.

    :param declaration: Decorator declaration.
    :param unit: Declaration scope.
    :param diagnostics: Destination for errors.
    :returns: None.
    """
    seen: set[str] = set()
    pair_seen = optional_seen = False
    resolver = Resolver(unit, diagnostics, {})
    for formal in parameters(declaration):
        if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', formal.name):
            resolver.issue(formal.node, 'decorator parameter names must use snake_case')
        if formal.name in seen:
            resolver.issue(formal.node, f'duplicate decorator parameter: {formal.name}')
        seen.add(formal.name)
        if (pair_seen and not formal.pair) or (optional_seen and not formal.optional):
            resolver.issue(
                formal.node, 'ordinary parameters precede pairs; optional pairs come last'
            )
        if (formal.optional and not formal.pair) or (formal.pair and formal.default is not None):
            resolver.issue(formal.node, 'only pairs use ?; only ordinary parameters use defaults')
        pair_seen |= formal.pair
        optional_seen |= formal.optional
        if 'self' not in map(str, tokens(formal.type)):
            expected = resolver.resolve(formal.type)
            if expected.kind == 'collection':
                resolver.issue(formal.type, 'decorator parameters cannot be collections')
            if formal.default is not None and 'self' not in map(str, tokens(formal.default)):
                argument_type(resolver, formal.default, expected)


def bind_call(
    call: Tree[Token], node: Tree[Token], unit: Unit, diagnostics: list[Diagnostic]
) -> str:
    """Bind one call and return its canonical signature.

    :param call: Decorator invocation.
    :param node: Decorated definition.
    :param unit: Invocation scope.
    :param diagnostics: Destination for errors.
    :returns: Identity and normalized arguments, or an empty string on lookup failure.
    """
    name = str(tokens(call)[1])
    actual_resolver = Resolver(unit, diagnostics, {}, bindings={'self': capture(node, unit)})
    symbol = unit.scope.get(name)
    if symbol is None or symbol.kind != 'decorator':
        actual_resolver.issue(call, f'unknown decorator: {name}')
        return ''
    scope, declaration = unit.world.declarations[symbol.key]
    formal_resolver = Resolver(scope, diagnostics, {}, bindings=actual_resolver.bindings)
    formals = parameters(declaration)
    ordinary = [p for p in formals if not p.pair]
    pairs = [p for p in formals if p.pair]
    supplied: dict[str, Tree[Token]] = {}
    position = pair_position = 0
    keyword_seen = pair_seen = False
    keys: set[str] = set()
    for argument in [c for c in call.children if isinstance(c, Tree)]:
        kind = argument.data
        if kind == 'pair_argument':
            pair_seen = True
            key = str(tokens(argument)[0])
            if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', key):
                actual_resolver.issue(argument, 'pair keys must use snake_case')
            if key in keys:
                actual_resolver.issue(argument, f'duplicate pair key: {key}')
            keys.add(key)
            formal = pairs[pair_position] if pair_position < len(pairs) else None
            pair_position += 1
        elif kind == 'keyword_argument':
            keyword_seen = True
            key = str(tokens(argument)[0])
            formal = next((p for p in ordinary if p.name == key), None)
        else:
            if keyword_seen:
                actual_resolver.issue(argument, 'positional arguments must precede named arguments')
            formal = ordinary[position] if position < len(ordinary) else None
            position += 1
        if pair_seen and kind != 'pair_argument':
            actual_resolver.issue(argument, 'new pairs must follow ordinary arguments')
        if formal is None:
            actual_resolver.issue(argument, f'unexpected argument for decorator: {name}')
        elif formal.name in supplied:
            actual_resolver.issue(argument, f'duplicate decorator argument: {formal.name}')
        else:
            supplied[formal.name] = argument
    bound: list[tuple[str, str, Resolved]] = []
    for formal in formals:
        expected = formal_resolver.resolve(formal.type)
        if expected.kind == 'collection':
            formal_resolver.issue(formal.type, 'decorator parameters cannot be collections')
        provided = supplied.get(formal.name)
        if provided is None:
            if formal.default is not None:
                actual = argument_type(formal_resolver, formal.default, expected)
                bound.append((formal.name, '', actual))
            elif not formal.optional:
                actual_resolver.issue(call, f'missing decorator argument: {formal.name}')
            continue
        syntax = children(provided, 'type_expr')[0]
        if formal.pair:
            actual = actual_resolver.resolve(syntax)
            if actual.kind == 'collection' or not accepts(expected, actual, unit.world):
                actual_resolver.issue(syntax, 'incompatible decorator pair type')
            bound.append((formal.name, str(tokens(provided)[0]), actual))
        else:
            bound.append((formal.name, '', argument_type(actual_resolver, syntax, expected)))
    return repr((symbol.key, bound))


def check_decorators(units: Sequence[Unit], diagnostics: list[Diagnostic]) -> None:
    """Validate calls, multiplicity and built-in placement rules.

    :param units: Complete resolved compilation unit.
    :param diagnostics: Destination for errors.
    :returns: None.
    """
    for unit in units:
        for declaration in [
            c
            for c in unit.document.tree.children
            if isinstance(c, Tree) and c.data not in ('namespace_decl', 'import_decl')
        ]:
            if declaration.data == 'decorator':
                check_parameters(declaration, unit, diagnostics)
            for node in [declaration, *children(declaration, 'field')]:
                seen: set[str] = set()
                dimensions: set[str] = set()
                body = children(node, 'field_body')
                mode = str(tokens(body[0])[0]) if body else ''
                for call in annotations(node):
                    name = str(tokens(call)[1])
                    bind_call(call, node, unit, diagnostics)
                    symbol = unit.scope.get(name)
                    if symbol is not None and symbol.kind == 'decorator':
                        definition = unit.world.declarations[symbol.key][1]
                        if name in seen and (
                            name == 'allow_multiple'
                            or not annotations(definition, 'allow_multiple')
                        ):
                            diagnostics.append(
                                diagnostic(unit.document, call, f'duplicate decorator: {name}')
                            )
                    seen.add(name)
                    message = ''
                    if name == 'allow_multiple' and node.data != 'decorator':
                        message = 'allow_multiple requires a decorator declaration'
                    elif name == 'flags' and node.data != 'decodes':
                        message = 'flags requires a Decodes declaration'
                    elif name in ('override', 'overload') and (
                        not body or mode == 'id' or declaration.data == 'value'
                    ):
                        message = 'field markers require an entity attr'
                    elif name == 'temporal' and mode not in ('snapshot', 'scd'):
                        message = 'temporal requires snapshot or scd attr'
                    elif name == 'dimension' and (
                        mode not in ('attr', 'snapshot', 'scd') or declaration.data == 'value'
                    ):
                        message = 'dimension requires attr, snapshot or scd'
                    if message:
                        diagnostics.append(diagnostic(unit.document, call, message))
                    if name in ('temporal', 'dimension'):
                        pairs = children(call, 'pair_argument')
                        if name == 'dimension' and pairs:
                            first = Resolver(
                                unit, [], {}, bindings={'self': capture(node, unit)}
                            ).resolve(children(pairs[0], 'type_expr')[0])
                            if first.kind != 'unknown' and first.name not in ('string', 'code'):
                                diagnostics.append(
                                    diagnostic(
                                        unit.document,
                                        pairs[0],
                                        'dimension value must be string or code',
                                    )
                                )
                        for pair in pairs:
                            key = str(tokens(pair)[0])
                            if key in dimensions:
                                diagnostics.append(
                                    diagnostic(unit.document, pair, f'duplicate dimension: {key}')
                                )
                            dimensions.add(key)
