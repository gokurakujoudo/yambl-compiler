"""Resolve named Hierarchy entries in their declaration scope."""

from dataclasses import dataclass

from lark import Token, Tree

from yambl_compiler.languages.yambl.semantics.decodes import is_flags
from yambl_compiler.languages.yambl.semantics.symbols import EntityKey, Unit
from yambl_compiler.languages.yambl.syntax.parser import (
    Diagnostic,
    children,
    diagnostic,
    string_value,
    tokens,
)


@dataclass(frozen=True)
class HierarchyEntry:
    """Retain the ordering and decoded code or string for one declaration entry.

    :param ordering: Unique ordering within this Hierarchy.
    :param value: String payload or dictionary code.
    :param dictionary: Resolved dictionary identity, or None for a string.
    :param node: Original declaration syntax.
    """

    ordering: int
    value: str
    dictionary: EntityKey | None
    node: Tree[Token]


def hierarchy_entries(
    unit: Unit, declaration: Tree[Token], diagnostics: list[Diagnostic]
) -> tuple[HierarchyEntry, ...]:
    """Resolve and validate a Hierarchy's entries without loading business data.

    :param unit: Declaration scope.
    :param declaration: Named Hierarchy syntax.
    :param diagnostics: Destination for entry errors.
    :returns: Entries in source order, including entries with diagnostics.
    """
    result: list[HierarchyEntry] = []
    seen: set[int] = set()
    for entry in children(declaration, 'hierarchy_entry'):
        items = tokens(entry)
        ordering = int(str(items[1]))
        if ordering in seen:
            diagnostics.append(diagnostic(unit.document, entry, 'duplicate Hierarchy ordering'))
        seen.add(ordering)
        if items[2].type == 'STRING':
            result.append(HierarchyEntry(ordering, string_value(items[2]), None, entry))
            continue
        name, value = str(items[2]), str(items[4])
        symbol = unit.scope.get(name)
        dictionary = unit.world.declarations.get(symbol.key) if symbol else None
        if symbol is None or symbol.kind != 'decodes' or dictionary is None:
            diagnostics.append(diagnostic(unit.document, entry, f'unknown Decodes: {name}'))
        elif is_flags(dictionary[1]):
            diagnostics.append(
                diagnostic(unit.document, entry, 'Hierarchy entries require ordinary Decodes')
            )
        elif value not in {str(tokens(item)[2]) for item in children(dictionary[1], 'entry')}:
            diagnostics.append(
                diagnostic(unit.document, entry, f'unknown Decodes code: {name}.{value}')
            )
        result.append(
            HierarchyEntry(
                ordering, value, symbol.key if symbol and symbol.kind == 'decodes' else None, entry
            )
        )
    return tuple(result)
