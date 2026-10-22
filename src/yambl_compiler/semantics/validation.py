"""Coordinate static checks across a complete compilation unit."""

from collections.abc import Sequence

from yambl_compiler.semantics.entities import check_entities
from yambl_compiler.semantics.fields import check_field, check_metadata
from yambl_compiler.semantics.symbols import declaration_name, resolve_scopes
from yambl_compiler.syntax.parser import Diagnostic, Document, children, diagnostic, tokens


def check(documents: Sequence[Document]) -> list[Diagnostic]:
    """Validate a compilation unit and report every unresolved Decodes reference.

    :param documents: Parsed files including referenced dependencies.
    :returns: Source-ordered errors and nonfatal external-dictionary warnings.
    """
    diagnostics: list[Diagnostic] = []
    units = resolve_scopes(documents, diagnostics)
    entities = check_entities(units, diagnostics)
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
            codes: set[str] = set()
            for entry in children(decodes, 'entry'):
                code = str(tokens(entry)[2])
                if code in codes:
                    diagnostics.append(
                        diagnostic(
                            unit.document, entry, f'duplicate code in Decodes identity: {code}'
                        )
                    )
                codes.add(code)
    return sorted(
        diagnostics, key=lambda item: (str(item.path), item.line, item.column, item.message)
    )
