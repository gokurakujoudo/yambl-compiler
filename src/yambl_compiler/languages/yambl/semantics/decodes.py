"""Validate dictionary codes and independent flag bits."""

import re
from decimal import Decimal

from lark import Token, Tree

from yambl_compiler.languages.yambl.semantics.symbols import EntityKey, SubtypeEntry, Unit, header
from yambl_compiler.languages.yambl.semantics.types import Resolved
from yambl_compiler.languages.yambl.syntax.parser import (
    Diagnostic,
    annotations,
    children,
    diagnostic,
    tokens,
)


def is_flags(declaration: Tree[Token]) -> bool:
    """Identify a dictionary with flags semantics.

    :param declaration: Dictionary declaration.
    :returns: Whether the flags marker is present.
    """
    return bool(annotations(declaration, 'flags'))


def collect_subtypes(units: list[Unit], diagnostics: list[Diagnostic]) -> None:
    """Derive independent depth-first dictionaries for every concrete entity.

    :param units: Resolved source units sharing an inheritance index.
    :param diagnostics: Destination for duplicate codes and rank placement errors.
    :returns: None; stores dictionary entries in the shared world.
    """
    if not units:
        return
    from yambl_compiler.languages.yambl.semantics.decorators import bind_call

    world = units[0].world
    entities = {k: v for k, v in world.declarations.items() if v[1].data == 'entity'}
    children_by_parent: dict[EntityKey, list[EntityKey]] = {}
    ranks: dict[EntityKey, tuple[int, str]] = {}
    for key, (unit, node) in entities.items():
        ancestors = world.parents.get(key, [])
        if ancestors:
            children_by_parent.setdefault(ancestors[0], []).append(key)
        calls = annotations(node, 'decodes_rank')
        if calls:
            if not children(header(node), 'inheritance'):
                diagnostics.append(
                    diagnostic(unit.document, calls[0], 'decodes_rank requires an explicit parent')
                )
            values: dict[str, Resolved] = {}
            bind_call(calls[0], node, unit, [], values)
            k, decode = values.get('k'), values.get('decode')
            if (
                k is not None
                and k.name == 'integer'
                and k.exact is not None
                and decode is not None
                and decode.name == 'string'
                and decode.exact is not None
            ):
                ranks[key] = (int(k.exact), decode.exact)
    for siblings in children_by_parent.values():
        siblings.sort(key=lambda key: (key[1], key[0]))
    world.subtypes.clear()
    for owner in entities:
        entries: list[SubtypeEntry] = []
        seen: set[EntityKey] = {owner}
        codes: set[str] = set()
        orderings: dict[EntityKey, int] = {}

        def visit(parent: EntityKey) -> None:
            """Visit direct descendants before moving to the next sibling.

            :param parent: Current dictionary member or owner.
            :returns: None; appends descendant entries.
            """
            for key in children_by_parent.get(parent, []):
                if key in seen:
                    continue
                seen.add(key)
                if key[1] in codes:
                    unit, node = entities[key]
                    diagnostics.append(
                        diagnostic(unit.document, header(node), f'duplicate subtype code: {key[1]}')
                    )
                codes.add(key[1])
                ordering = (len(entries) + 1) * 100
                decode = key[1]
                if key in ranks:
                    k, decode = ranks[key]
                    ordering = orderings.get(parent, 0) + k
                entries.append(SubtypeEntry(key, key[1], decode, ordering))
                orderings[key] = ordering
                visit(key)

        visit(owner)
        world.subtypes[(owner[0], owner[1] + '::subtypes')] = tuple(entries)


def check_decodes(node: Tree[Token], unit: Unit, diagnostics: list[Diagnostic]) -> None:
    """Check code spelling, uniqueness and explicit flag masks.

    :param node: Dictionary declaration.
    :param unit: Original source scope.
    :param diagnostics: Destination for located errors.
    :returns: None.
    """
    markers = annotations(node, 'flags')
    if len(markers) > 1:
        diagnostics.append(diagnostic(unit.document, markers[-1], 'duplicate flags annotation'))
    codes: set[str] = set()
    masks: set[int] = set()
    for entry in children(node, 'entry'):
        order, code = map(str, tokens(entry)[1:3])
        if not re.fullmatch(r'[A-Z][A-Za-z0-9]*', code):
            diagnostics.append(
                diagnostic(unit.document, entry, 'Decodes codes must use PascalCase')
            )
        if code in codes:
            diagnostics.append(
                diagnostic(unit.document, entry, f'duplicate code in Decodes: {code}')
            )
        codes.add(code)
        if markers:
            if code in ('None', 'All'):
                diagnostics.append(
                    diagnostic(
                        unit.document, entry, f'flags code {code} is generated automatically'
                    )
                )
            mask = int(order, 2) if '0b' in order else int(Decimal(order))
            if mask <= 0 or mask & (mask - 1):
                diagnostics.append(
                    diagnostic(
                        unit.document, entry, 'flags entries require a positive single-bit mask'
                    )
                )
            if mask in masks:
                diagnostics.append(diagnostic(unit.document, entry, 'duplicate flags mask'))
            masks.add(mask)
