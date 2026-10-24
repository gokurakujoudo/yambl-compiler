"""Coordinate static checks across a complete compilation unit."""

from collections.abc import Sequence

from yambl_compiler.semantics.decodes import check_decodes
from yambl_compiler.semantics.entities import check_entities
from yambl_compiler.semantics.expressions import check_aliases
from yambl_compiler.semantics.fields import check_field, check_metadata
from yambl_compiler.semantics.symbols import declaration_name, resolve_scopes
from yambl_compiler.semantics.values import check_values
from yambl_compiler.syntax.parser import Diagnostic, Document, children


def check(documents: Sequence[Document]) -> list[Diagnostic]:
    """Validate a compilation unit and report every unresolved Decodes reference.

    :param documents: Parsed files including referenced dependencies.
    :returns: Source-ordered errors and nonfatal external-dictionary warnings.
    """
    diagnostics: list[Diagnostic] = []
    units = resolve_scopes(documents, diagnostics)
    entities = check_entities(units, diagnostics)
    check_aliases(units, diagnostics)
    check_values(units, diagnostics)
    for unit in units:
        for node in [
            *children(unit.document.tree, 'entity'),
            *children(unit.document.tree, 'extension'),
        ]:
            check_metadata(node, unit, diagnostics)
            symbol = unit.scope.get(declaration_name(node))
            self_fields = entities.field_types(symbol.key) if symbol is not None else {}
            for field in children(node, 'field'):
                check_field(
                    field,
                    unit,
                    diagnostics,
                    extension=node.data == 'extension',
                    self_fields=self_fields,
                )
        for decodes in children(unit.document.tree, 'decodes'):
            check_metadata(decodes, unit, diagnostics)
            check_decodes(decodes, unit, diagnostics)
    return sorted(
        diagnostics, key=lambda item: (str(item.path), item.line, item.column, item.message)
    )
