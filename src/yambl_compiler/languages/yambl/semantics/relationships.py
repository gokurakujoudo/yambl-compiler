"""Validate stored relationships and their derived reverse attributes."""

from collections.abc import Sequence

from lark import Token, Tree

from yambl_compiler.languages.yambl.semantics.decorators import bind_call
from yambl_compiler.languages.yambl.semantics.dimensions import field_dimension
from yambl_compiler.languages.yambl.semantics.entities import EntityIndex, FieldSite
from yambl_compiler.languages.yambl.semantics.expressions import Resolver
from yambl_compiler.languages.yambl.semantics.symbols import EntityKey, Unit, declaration_name
from yambl_compiler.languages.yambl.semantics.types import Resolved
from yambl_compiler.languages.yambl.syntax.parser import (
    Diagnostic,
    annotations,
    children,
    diagnostic,
    field_mode,
    tokens,
)


def field_type(site: FieldSite, index: EntityIndex, owner: tuple[str, str]) -> Resolved:
    """Resolve a field in its declaration scope.

    :param site: Original field declaration and scope.
    :param index: Effective field index.
    :param owner: Entity whose field contract is being resolved.
    :returns: Expanded field type.
    """
    body = children(site.declaration, 'field_body')[0]
    return Resolver(site.unit, [], index.field_types(owner), True).resolve(
        children(body, 'type_expr')[0]
    )


def dimensions(site: FieldSite) -> set[tuple[str, str]]:
    """Collect identities of the dimensions used by a field.

    :param site: Original field declaration and scope.
    :returns: Declared dimension identities.
    """
    return {
        dimension.identity
        for call in annotations(site.declaration, 'dimension')
        if (dimension := field_dimension(site.unit, site.declaration, call, [])) is not None
    }


def temporal(site: FieldSite) -> tuple[tuple[str, str], ...]:
    """Read the time parameter contract in declaration scope.

    :param site: Original field declaration and scope.
    :returns: Temporal parameter names and resolved types.
    """
    result: list[tuple[str, str]] = []
    for call in annotations(site.declaration, 'temporal'):
        bound: list[tuple[str, str, Resolved]] = []
        bind_call(call, site.declaration, site.unit, [], bound_arguments=bound)
        result.extend((key, repr(value)) for name, key, value in bound if name == 'time')
    return tuple(result)


def reverse_path(
    unit: Unit, syntax: Tree[Token], index: EntityIndex
) -> tuple[EntityKey | None, list[tuple[EntityKey, str, FieldSite]]]:
    """Resolve an ordered reverse path in its declaration scope.

    :param unit: Invocation scope.
    :param syntax: Source path expression.
    :param index: Effective entity fields.
    :returns: Starting entity and every resolvable forward step.
    """
    items = tokens(syntax)
    if (
        len(items) < 3
        or len(items) % 2 == 0
        or any(str(items[i]) != '.' for i in range(1, len(items), 2))
    ):
        return None, []
    symbol = unit.scope.get(str(items[0]))
    if symbol is None or symbol.kind != 'entity':
        return None, []
    current = symbol.key
    steps: list[tuple[EntityKey, str, FieldSite]] = []
    for item in items[2::2]:
        site = index.effective.get(current, {}).get(str(item))
        if site is None:
            break
        steps.append((current, str(item), site))
        resolved = field_type(site, index, current)
        target = resolved.args[0] if resolved.name == 'collection' and resolved.args else resolved
        if target.kind != 'reference' or target.key is None:
            break
        current = target.key
    return symbol.key, steps


def check_relationships(
    units: Sequence[Unit], index: EntityIndex, diagnostics: list[Diagnostic]
) -> None:
    """Check the paired declarations after all effective fields are known.

    :param units: Model declaration scopes.
    :param index: Effective fields and inheritance.
    :param diagnostics: Destination for source errors.
    :returns: None.
    """
    for unit in units:
        for declaration in [
            *children(unit.document.tree, 'entity'),
            *children(unit.document.tree, 'extension'),
        ]:
            symbol = unit.scope.get(declaration_name(declaration))
            if symbol is None:
                continue
            owner = symbol.key
            for field in children(declaration, 'field'):
                site = FieldSite(unit, field)
                forward = annotations(field, 'relationship')
                reverse = annotations(field, 'reverse_relationship')
                if not forward and not reverse:
                    continue
                body = children(field, 'field_body')[0]
                mode = field_mode(body)

                def issue(node: Tree[Token], message: str) -> None:
                    """Report a relationship error at its source.

                    :param node: Invalid source syntax.
                    :param message: Diagnostic message.
                    :returns: None.
                    """
                    diagnostics.append(diagnostic(unit.document, node, message))

                if reverse and (
                    forward or annotations(field, 'sparse') or annotations(field, 'dense')
                ):
                    issue(
                        reverse[0],
                        'reverse relationship cannot have forward or storage layout annotations',
                    )
                for annotation in annotations(field, 'dimension'):
                    dimension = field_dimension(unit, field, annotation, [])
                    if dimension is not None and (
                        dimension.hierarchy_name or dimension.fallback is not None
                    ):
                        issue(
                            annotation,
                            'relationship dimensions cannot have hierarchy or fallback',
                        )
                actual = field_type(site, index, owner)
                target = actual.args[0] if actual.kind == 'collection' and actual.args else actual
                if forward:
                    bound: dict[str, Resolved] = {}
                    bind_call(forward[0], field, unit, [], values=bound)
                    y = bound.get('y', Resolved('?')).exact
                    if target.kind != 'reference' or (
                        actual.kind != 'reference' and actual.name != 'collection'
                    ):
                        issue(
                            forward[0],
                            'relationship requires an entity reference or collection of entity references',
                        )
                    elif (actual.kind == 'reference' and y not in ('maybe', 'one')) or (
                        actual.name == 'collection' and y not in ('maybe', 'many')
                    ):
                        if y is not None:
                            issue(forward[0], 'relationship cardinality does not match field type')
                if not reverse:
                    continue
                args = [
                    part
                    for part in reverse[0].children
                    if isinstance(part, Tree)
                    and part.data in ('positional_argument', 'keyword_argument')
                ]
                if not args:
                    continue
                syntax = children(args[0], 'type_expr')[0]
                items = tokens(syntax)
                source, steps = reverse_path(unit, syntax, index)
                if source is None:
                    issue(
                        reverse[0],
                        'reverse relationship target must be a forward relationship field',
                    )
                    continue
                if len(steps) != (len(items) - 1) // 2:
                    if len(items) > 3:
                        position = min(2 + len(steps) * 2, len(items) - 1)
                        missing = items[position]
                        diagnostics.append(
                            diagnostic(
                                unit.document,
                                Tree('reverse_hop', [missing]),
                                f'reverse relationship hop {len(steps) + 1} {missing} is not a visible field',
                            )
                        )
                    else:
                        issue(
                            reverse[0],
                            'reverse relationship target must be a forward relationship field',
                        )
                    continue
                source_target = Resolved('?')
                x = None
                valid = True
                for number, (step_owner, step_name, forward_site) in enumerate(steps, 1):
                    relationships = annotations(forward_site.declaration, 'relationship')
                    if not relationships or annotations(
                        forward_site.declaration, 'reverse_relationship'
                    ):
                        issue(
                            reverse[0],
                            (
                                f'reverse relationship hop {number} {step_name} must be a forward relationship field'
                                if len(steps) > 1
                                else 'reverse relationship target must be a forward relationship field'
                            ),
                        )
                        valid = False
                        break
                    source_type = field_type(forward_site, index, step_owner)
                    source_target = (
                        source_type.args[0]
                        if source_type.kind == 'collection' and source_type.args
                        else source_type
                    )
                    step_bound: dict[str, Resolved] = {}
                    bind_call(
                        relationships[0],
                        forward_site.declaration,
                        forward_site.unit,
                        [],
                        values=step_bound,
                    )
                    x = step_bound.get('x', Resolved('?')).exact
                    y = step_bound.get('y', Resolved('?')).exact
                    if len(steps) > 1 and (
                        source_type.kind != 'reference' or x != 'one' or y not in ('one', 'maybe')
                    ):
                        issue(
                            reverse[0],
                            f'reverse relationship hop {number} {step_name} requires a one-to-one reference',
                        )
                        valid = False
                    source_body = children(forward_site.declaration, 'field_body')[0]
                    if mode != field_mode(source_body) or temporal(site) != temporal(forward_site):
                        issue(
                            reverse[0],
                            (
                                f'relationship categories and temporal parameters must match at hop {number} {step_name}'
                                if len(steps) > 1
                                else 'relationship categories and temporal parameters must match'
                            ),
                        )
                    if dimensions(site) != dimensions(forward_site):
                        issue(
                            reverse[0],
                            (
                                f'relationship dimensions must match by declaration identity at hop {number} {step_name}'
                                if len(steps) > 1
                                else 'relationship dimensions must match by declaration identity'
                            ),
                        )
                if not valid:
                    continue
                if source_target.key != owner:
                    issue(reverse[0], 'forward relationship target must match reverse owner')
                if (
                    target.kind != 'reference'
                    or target.key != source
                    or (x == 'one' and actual.kind != 'reference')
                    or (x == 'many' and actual.name != 'collection')
                ):
                    issue(reverse[0], 'reverse relationship type does not match source cardinality')
