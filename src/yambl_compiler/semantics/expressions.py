"""Resolve lexical type aliases and instance-rooted field expressions."""

import re
from dataclasses import dataclass, replace
from typing import Literal

from lark import Token, Tree

from yambl_compiler.semantics.builtins import RESULT_CONSTANTS
from yambl_compiler.semantics.decodes import is_flags
from yambl_compiler.semantics.symbols import (
    GENERICS,
    RESERVED,
    VALUES,
    Unit,
    field_owner,
    header,
    symbol_key,
)
from yambl_compiler.semantics.types import (
    ENTITY,
    UNKNOWN,
    FieldTypes,
    Resolved,
    accepts,
    common_type,
)
from yambl_compiler.syntax.parser import Diagnostic, children, diagnostic, string_value, tokens


@dataclass(frozen=True)
class Term:
    """Distinguish a type, a value and a field description.

    :param type: Type or value's declared type.
    :param sort: Expression category.
    :param path: Instance path or symbolic key binding.
    :param source: Field's type syntax and original scope.
    :param instance: Whether a field came from an instance.
    """

    type: Resolved
    sort: Literal['type', 'value', 'field'] = 'type'
    path: str = ''
    source: tuple[Unit, Tree[Token]] | None = None
    instance: bool = False


class Resolver:
    """Resolve expressions with explicit lexical and instance environments."""

    def __init__(
        self,
        unit: Unit,
        diagnostics: list[Diagnostic],
        self_fields: FieldTypes,
        allow_exact: bool = False,
        bindings: dict[str, Term] | None = None,
        active: tuple[str, ...] = (),
    ) -> None:
        """Retain contexts required for dependent type resolution.

        :param unit: Original lexical scope.
        :param diagnostics: Shared diagnostic destination.
        :param self_fields: Effective fields of the current instance.
        :param allow_exact: Whether singleton constraints are permitted here.
        :param bindings: Explicit generic parameters and mapping key bindings.
        :param active: Definitions already being expanded.
        :returns: None.
        """
        self.unit = unit
        self.diagnostics = diagnostics
        self.self_fields = self_fields
        self.allow_exact = allow_exact
        self.bindings = bindings if bindings is not None else {}
        self.active = active

    def issue(self, node: Tree[Token], message: str, warning: bool = False) -> None:
        """Report one located expression problem.

        :param node: Offending expression.
        :param message: Diagnostic text.
        :param warning: Whether this is an external dictionary warning.
        :returns: None.
        """
        item = diagnostic(self.unit.document, node, message, 'warning' if warning else 'error')
        if item not in self.diagnostics:
            self.diagnostics.append(item)

    def resolve(self, node: Tree[Token]) -> Resolved:
        """Interpret an expression in a type position.

        :param node: Type syntax.
        :returns: Resolved type, or unknown after a diagnostic.
        """
        return self.as_type(self.evaluate(node), node)

    def as_type(self, term: Term, node: Tree[Token]) -> Resolved:
        """Require a type or a value carrying an entity type.

        :param term: Evaluated expression.
        :param node: Diagnostic site.
        :returns: Selected type.
        """
        if term.sort == 'type':
            return term.type
        if term.sort == 'value' and term.type.name == 'type':
            target = term.type.args[0]
            return replace(target, dependency=term.path) if term.type.exact is None else target
        self.issue(node, 'type position requires a type or an entity type value')
        return UNKNOWN

    def value_type(self, term: Term) -> Resolved:
        """Treat entity names as type-valued constants in argument positions.

        :param term: Evaluated argument.
        :returns: Value contract used for parameter checking.
        """
        if term.sort == 'type' and term.type.kind == 'reference':
            return Resolved('type', args=(term.type,), exact=repr(term.type.key))
        return term.type if term.sort == 'value' else UNKNOWN

    def evaluate(self, node: Tree[Token], allow_binding: bool = False) -> Term:
        """Evaluate syntax without executing model instances.

        :param node: Expression syntax.
        :param allow_binding: Whether this expression declares a mapping key binding.
        :returns: Type, symbolic value or field description.
        """
        if children(node, 'binding') and not allow_binding:
            self.issue(node, 'as binding is only allowed on a mapping key')
        first = tokens(node)[0]
        name = str(first)
        if first.type == 'NAME' and name in RESULT_CONSTANTS:
            self.issue(node, f'result constant {name} cannot be used in model expressions')
            return Term(UNKNOWN)
        groups = children(node, 'arguments')
        args = children(groups[0], 'type_expr') if groups else []
        params = children(node, 'parameters')
        if (groups or params) and (
            name == 'self' or first.type in ('STRING', 'INT', 'NUMBER', 'BOOLEAN')
        ):
            self.issue(node, 'a value expression does not accept type arguments')
        if name in self.bindings:
            result = self.bindings[name]
            if groups or params:
                self.issue(node, 'a bound parameter does not accept arguments')
        elif name == 'self':
            if not self.self_fields:
                self.issue(
                    node, 'self requires an instance context; pass alias dependencies as arguments'
                )
            result = Term(ENTITY, 'value', 'self')
        elif first.type in ('STRING', 'INT', 'NUMBER', 'BOOLEAN'):
            base = {'STRING': 'string', 'INT': 'integer', 'NUMBER': 'decimal', 'BOOLEAN': 'bool'}[
                first.type
            ]
            value = string_value(first) if first.type == 'STRING' else str(first)
            result = Term(
                Resolved(base, 'string' if base == 'string' else 'value', exact=value), 'value'
            )
        elif name in GENERICS:
            result = self.generic(node, name, args, bool(params))
        else:
            symbol = self.unit.scope.get(name)
            if symbol is not None and symbol.kind == 'type_alias':
                result = self.expand(node, symbol.key, args, bool(params))
            else:
                if groups:
                    self.issue(node, f'{name} does not accept generic arguments')
                if name in VALUES:
                    result = Term(
                        Resolved(
                            name,
                            'string' if name == 'string' else 'value',
                            params=self.parameters(node, name),
                        )
                    )
                elif name == 'Entity':
                    if params:
                        self.issue(node, 'invalid parameters for Entity')
                    result = Term(ENTITY)
                elif symbol is not None and symbol.kind != 'decorator':
                    if params:
                        self.issue(
                            node,
                            f'invalid parameters for {name}; do not mix positional and named parameters',
                        )
                    if symbol.kind == 'decodes':
                        result = Term(
                            Resolved(
                                'Decodes',
                                'reference',
                                key=self.unit.scope['Decodes'].key,
                                exact=repr(symbol.key),
                            ),
                            'value',
                            name,
                        )
                    else:
                        result = Term(
                            Resolved(
                                name,
                                'reference' if symbol.kind == 'entity' else 'value',
                                key=symbol.key,
                            )
                        )
                else:
                    self.issue(node, f'unknown type: {name}')
                    result = Term(UNKNOWN)
        for access in children(node, 'access'):
            result = self.access(result, access)
        for operation in children(node, 'flag_operation'):
            result = self.combine(
                result, self.evaluate(children(operation, 'flag_operand')[0]), operation
            )
        return result

    def combine(self, left: Term, right: Term, node: Tree[Token]) -> Term:
        """Combine codes from one flags dictionary without executing instances.

        :param left: Accumulated flags value.
        :param right: Next flags value.
        :param node: Union or difference site.
        :returns: Canonical constant set or a symbolic flags value.
        """
        operator = str(tokens(node)[0])
        if (
            left.sort != 'value'
            or right.sort != 'value'
            or left.type.name != 'code'
            or right.type.name != 'code'
            or left.type.base() != right.type.base()
        ):
            self.issue(node, f'{operator} requires codes from the same flags Decodes')
            return Term(UNKNOWN)
        declaration = self.unit.world.declarations.get(left.type.args[0].key or ('', ''))
        if declaration is None or not is_flags(declaration[1]):
            self.issue(node, f'{operator} requires codes from the same flags Decodes')
            return Term(UNKNOWN)
        exact = None
        if left.type.exact is not None and right.type.exact is not None:
            first = set(filter(None, left.type.exact.split('+')))
            second = set(filter(None, right.type.exact.split('+')))
            exact = '+'.join(sorted(first | second if operator == '+' else first - second))
        return Term(replace(left.type, exact=exact), 'value', left.path + operator + right.path)

    def parameters(self, node: Tree[Token], name: str) -> tuple[tuple[str, str], ...]:
        """Validate string and decimal descriptor parameters.

        :param node: Type expression.
        :param name: Primitive name.
        :returns: Canonical parameter pairs.
        """
        groups = children(node, 'parameters')
        if not groups:
            return ()
        items = groups[0].children[1:-1]
        named = [item for item in items if isinstance(item, Tree)]
        literals = [
            item for item in items if isinstance(item, Token) and item.type in ('INT', 'STRING')
        ]
        pairs: list[tuple[str, Token]] = []
        entries: list[Tree[Token] | Token] = list(named) if named else list(literals)
        for index, item in enumerate(entries):
            if isinstance(item, Tree):
                entry = tokens(item)
                pairs.append((str(entry[0]), entry[-1]))
            else:
                pairs.append(
                    (('precision', 'scale')[min(index, 1)] if name == 'decimal' else 'regex', item)
                )
        expected = {'regex'} if name == 'string' else {'precision', 'scale'}
        valid = (
            name in ('string', 'decimal')
            and not (named and literals)
            and len(pairs) == len(expected)
            and {key for key, _ in pairs} == expected
            and all(value.type == ('STRING' if name == 'string' else 'INT') for _, value in pairs)
        )
        if not valid:
            self.issue(
                node, f'invalid parameters for {name}; do not mix positional and named parameters'
            )
        return tuple(
            sorted(
                (key, string_value(value) if value.type == 'STRING' else integer_text(str(value)))
                for key, value in pairs
            )
        )

    def expand(
        self, node: Tree[Token], key: tuple[str, str], args: list[Tree[Token]], parameters: bool
    ) -> Term:
        """Instantiate a transparent definition with lexical substitution.

        :param node: Invocation site.
        :param key: Alias identity.
        :param args: Actual arguments.
        :param parameters: Whether square-bracket arguments were supplied.
        :returns: Expanded type expression.
        """
        identity = repr(key)
        if identity in self.active:
            self.issue(node, 'type dependency cycle')
            return Term(UNKNOWN)
        scope, declaration = self.unit.world.declarations[key]
        groups = children(header(declaration), 'type_parameters')
        formals = children(groups[0], 'type_parameter') if groups else []
        if parameters or len(args) != len(formals):
            self.issue(node, f'invalid type arguments for {key[1]}')
            return Term(UNKNOWN)
        bindings: dict[str, Term] = {}
        nested = Resolver(
            scope, self.diagnostics, {}, self.allow_exact, bindings, (*self.active, identity)
        )
        valid = True
        for formal, arg in zip(formals, args):
            expected = nested.resolve(children(formal, 'type_expr')[0])
            actual = self.evaluate(arg)
            supplied = self.value_type(actual)
            if not accepts(expected, supplied, self.unit.world) or supplied.kind == 'unknown':
                self.issue(arg, f'incompatible argument for {key[1]}.{tokens(formal)[0]}')
                valid = False
            bindings[str(tokens(formal)[0])] = actual
        return nested.evaluate(children(declaration, 'type_expr')[0]) if valid else Term(UNKNOWN)

    def access(self, term: Term, node: Tree[Token]) -> Term:
        """Resolve one dot or double-colon access.

        :param term: Expression preceding the access.
        :param node: Access operator and member name.
        :returns: Member expression.
        """
        operator, member = map(str, tokens(node))
        path = term.path + operator + member
        if operator == '::':
            if term.sort == 'field' and member == 'entity':
                owner = field_owner(*term.source) if term.source else None
                if owner is None or owner.kind != 'entity':
                    self.issue(node, 'field description has no owning entity')
                    return Term(UNKNOWN)
                return Term(Resolved(owner.key[1], 'reference', key=owner.key))
            if term.sort != 'field' or member not in ('type', 'value'):
                self.issue(node, ':: requires a field description and type or value')
                return Term(UNKNOWN)
            if member == 'value' and not term.instance:
                self.issue(node, 'field value requires an instance origin')
                return Term(UNKNOWN)
            if term.source is None:
                return Term(
                    Resolved('dependent', args=term.type.members, dependency=term.path + '::type')
                )
            scope, syntax = term.source
            identity = f'{scope.document.path}:{tokens(syntax)[0].start_pos}'
            if identity in self.active:
                self.issue(node, 'type dependency cycle')
                return Term(UNKNOWN)
            context = self.self_fields
            for fields in scope.world.fields.values():
                for field_scope, field in fields.values():
                    if (
                        field_scope is scope
                        and children(children(field, 'field_body')[0], 'type_expr')[0] is syntax
                    ):
                        context = {
                            name: (s, children(children(f, 'field_body')[0], 'type_expr')[0])
                            for name, (s, f) in fields.items()
                        }
            resolved = Resolver(
                scope, self.diagnostics, context, True, active=(*self.active, identity)
            ).resolve(syntax)
            return Term(resolved, 'type' if member == 'type' else 'value', path)
        if term.type.name == 'Decodes' and term.type.exact is not None:
            key = next(key for key in self.unit.world.declarations if repr(key) == term.type.exact)
            _, declaration = self.unit.world.declarations[key]
            codes = {str(tokens(entry)[2]) for entry in children(declaration, 'entry')}
            exact = member
            if is_flags(declaration):
                if member == 'All':
                    exact = '+'.join(sorted(codes))
                elif member == 'None':
                    exact = ''
                codes.update(('None', 'All'))
            if member not in codes:
                self.issue(node, f'unknown Decodes code: {term.path}.{member}')
            return Term(
                Resolved(
                    'code',
                    args=(Resolved(key[1], key=key),),
                    exact=exact,
                ),
                'value',
                path,
            )
        if term.type.kind != 'reference' or term.sort == 'field':
            self.issue(node, 'field access requires an entity instance or entity type')
            return Term(UNKNOWN)
        if term.path == 'self':
            source = self.self_fields.get(member)
        else:
            fields = self.unit.world.fields.get(term.type.key or ('', ''), {})
            site = fields.get(member)
            source = (
                (site[0], children(children(site[1], 'field_body')[0], 'type_expr')[0])
                if site
                else None
            )
        if source is None:
            self.issue(node, f'unknown field: {member}')
            return Term(UNKNOWN)
        return Term(Resolved('attrs'), 'field', path, source, term.sort == 'value')

    def generic(
        self, node: Tree[Token], name: str, args: list[Tree[Token]], parameters: bool
    ) -> Term:
        """Resolve a core type operator.

        :param node: Operator invocation.
        :param name: Primitive name.
        :param args: Argument expressions.
        :param parameters: Whether descriptor parameters were supplied.
        :returns: Type produced by the operator.
        """
        if parameters or len(args) != (2 if name in ('mapping', 'attrs') else 1):
            self.issue(node, f'invalid type arguments for {name}')
            return Term(UNKNOWN)
        if name == 'code':
            return Term(self.code(node, args[0]))
        if name in ('alias', 'exact'):
            value = self.evaluate(args[0])
            resolved = self.value_type(value)
            if name == 'alias':
                if (
                    value.sort != 'value'
                    or not value.path.endswith('::value')
                    or value.type.kind == 'unknown'
                ):
                    self.issue(node, 'alias requires an instance field value')
                return Term(replace(resolved, dependency=value.path))
            if not self.allow_exact:
                self.issue(node, 'exact requires override or overload')
            if resolved.exact is None:
                self.issue(node, 'exact requires a constant value')
            return Term(resolved)
        if name == 'type':
            target = self.resolve(args[0])
            if target.kind not in ('reference', 'unknown'):
                self.issue(node, 'type requires Entity or a visible entity type')
            return Term(Resolved('type', args=(target,)))
        if name == 'attrs':
            return Term(self.attributes(node, args))
        first = self.as_type(self.evaluate(args[0], name == 'mapping'), args[0])
        if name == 'mapping':
            bound = children(args[0], 'binding')
            bindings = dict(self.bindings)
            if bound:
                bindings[str(tokens(bound[0])[-1])] = Term(
                    first, 'field' if first.name == 'attrs' else 'value', '$key'
                )
            second = Resolver(
                self.unit,
                self.diagnostics,
                self.self_fields,
                self.allow_exact,
                bindings,
                self.active,
            ).resolve(args[1])
            if first.kind not in ('reference', 'unknown') and not first.string_like():
                self.issue(node, 'mapping keys must be string-compatible or entity references')
            if second.kind == 'collection':
                self.issue(node, 'mapping values cannot be collections')
            return Term(Resolved(name, 'collection', (first, second)))
        if name == 'collection' and first.kind not in ('reference', 'unknown'):
            self.issue(node, 'collection requires an entity reference')
        if name == 'values' and first.kind not in ('value', 'string', 'unknown'):
            self.issue(node, 'values requires a value type')
        return Term(Resolved(name, 'collection', (first,)))

    def attributes(self, node: Tree[Token], args: list[Tree[Token]]) -> Resolved:
        """Select non-collection attribute contracts within a business tree.

        :param node: Attrs operator.
        :param args: Category and entity type arguments.
        :returns: Field identity type and its possible value contracts.
        """
        category = self.evaluate(args[0])
        target = self.resolve(args[1])
        expected = Resolved(
            'code', args=(Resolved('AttrCats', key=self.unit.scope['AttrCats'].key),)
        )
        if (
            not accepts(expected, self.value_type(category), self.unit.world)
            or category.sort != 'value'
        ):
            self.issue(node, 'attrs requires an AttrCats code')
        if target.kind not in ('reference', 'unknown'):
            self.issue(node, 'attrs requires an entity type value')
        members: dict[str, list[Resolved]] = {}
        for key, fields in self.unit.world.fields.items():
            if target.key != key and target.key not in self.unit.world.parents.get(key, []):
                continue
            for field_name, (scope, field) in fields.items():
                body = children(field, 'field_body')[0]
                mode = str(tokens(body)[0])
                if mode == 'id' or (
                    category.type.exact is not None
                    and mode.capitalize() not in category.type.exact.split('+')
                ):
                    continue
                syntax = children(body, 'type_expr')[0]
                identity = f'{scope.document.path}:{tokens(syntax)[0].start_pos}'
                if identity in self.active:
                    continue
                context = {
                    n: (s, children(children(f, 'field_body')[0], 'type_expr')[0])
                    for n, (s, f) in fields.items()
                }
                resolved = Resolver(
                    scope, [], context, True, active=(*self.active, identity)
                ).resolve(syntax)
                if resolved.kind not in ('collection', 'unknown'):
                    members.setdefault(field_name, []).append(resolved)
        return Resolved(
            'attrs',
            args=(category.type, target),
            members=tuple(
                common_type(group, self.unit.world) or UNKNOWN for group in members.values()
            ),
        )

    def code(self, node: Tree[Token], argument: Tree[Token]) -> Resolved:
        """Resolve a static or instance-selected dictionary.

        :param node: Code operator for diagnostics.
        :param argument: Dictionary name or value path.
        :returns: Dictionary-constrained code type.
        """
        items = tokens(argument)
        if len(items) == 1 and items[0].type in ('NAME', 'STRING'):
            name = string_value(items[0]) if items[0].type == 'STRING' else str(items[0])
            if name not in self.bindings:
                symbol = self.unit.scope.get(name)
                if (symbol is not None and symbol.kind != 'decodes') or (
                    symbol is None and name in RESERVED
                ):
                    self.issue(node, 'code requires a Decodes instance identifier')
                elif symbol is None:
                    quoted = items[0].type == 'STRING'
                    self.issue(node if quoted else argument, f'undefined Decodes: {name}', quoted)
                return Resolved(
                    'code',
                    args=(
                        Resolved(
                            name,
                            key=symbol.key if symbol else symbol_key(self.unit.document, name),
                        ),
                    ),
                )
        value = self.evaluate(argument)
        if value.sort != 'value' or value.type.name != 'Decodes':
            self.issue(node, 'code requires a Decodes instance value')
        if value.type.name == 'Decodes' and value.type.exact is not None:
            key = next(key for key in self.unit.world.declarations if repr(key) == value.type.exact)
            return Resolved('code', args=(Resolved(key[1], key=key),))
        return Resolved('code', args=(value.type,), dependency=value.path)


def check_aliases(units: list[Unit], diagnostics: list[Diagnostic]) -> None:
    """Validate alias definitions even when no field instantiates them.

    :param units: User compilation units.
    :param diagnostics: Destination for errors.
    :returns: None.
    """
    for unit in units:
        for declaration in children(unit.document.tree, 'type_alias'):
            bindings: dict[str, Term] = {}
            resolver = Resolver(unit, diagnostics, {}, True, bindings)
            groups = children(header(declaration), 'type_parameters')
            for formal in children(groups[0], 'type_parameter') if groups else []:
                name = str(tokens(formal)[0])
                if name in bindings or not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', name):
                    resolver.issue(formal, f'duplicate or invalid type parameter: {name}')
                constraint = resolver.resolve(children(formal, 'type_expr')[0])
                bindings[name] = Term(constraint, 'value', '$' + name)
            resolver.resolve(children(declaration, 'type_expr')[0])


def integer_text(value: str) -> str:
    """Normalize integer spelling without a bounded integer conversion.

    :param value: Signed decimal integer token.
    :returns: Canonical sign and digits.
    """
    digits = value.lstrip('-0') or '0'
    return '-' + digits if value.startswith('-') and digits != '0' else digits
