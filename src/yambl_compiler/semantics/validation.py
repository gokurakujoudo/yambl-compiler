"""Coordinate static checks across a complete compilation unit."""

from collections.abc import Sequence
from dataclasses import dataclass

from yambl_compiler.semantics.decodes import check_decodes
from yambl_compiler.semantics.decorators import check_decorators
from yambl_compiler.semantics.entities import EntityIndex, check_entities
from yambl_compiler.semantics.expressions import check_aliases
from yambl_compiler.semantics.fields import check_field
from yambl_compiler.semantics.symbols import Unit, declaration_name, resolve_scopes
from yambl_compiler.semantics.values import check_values
from yambl_compiler.syntax.parser import Diagnostic, Document, children


@dataclass(frozen=True)
class Analysis:
    """Retain checked scopes and relationships for semantic consumers.

    :param units: Analyzed compilation units.
    :param entities: Effective fields and logical identities.
    :param diagnostics: Ordered errors and warnings.
    """

    units: tuple[Unit, ...]
    entities: EntityIndex
    diagnostics: tuple[Diagnostic, ...]


def analyze_units(units: list[Unit], diagnostics: list[Diagnostic]) -> Analysis:
    """Validate a compilation unit and report every unresolved Decodes reference.

    :param units: Resolved scopes, including embedded definitions when loading IR.
    :param diagnostics: Existing scope diagnostics.
    :returns: Checked semantic model and source-ordered diagnostics.
    """
    entities = check_entities(units, diagnostics)
    check_decorators(units, diagnostics)
    check_aliases(units, diagnostics)
    check_values(units, diagnostics)
    for unit in units:
        for node in [
            *children(unit.document.tree, 'entity'),
            *children(unit.document.tree, 'extension'),
        ]:
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
            check_decodes(decodes, unit, diagnostics)
    return Analysis(
        tuple(units),
        entities,
        tuple(
            sorted(
                diagnostics, key=lambda item: (str(item.path), item.line, item.column, item.message)
            )
        ),
    )


def analyze(documents: Sequence[Document]) -> Analysis:
    """Resolve and check source documents with installed language definitions.

    :param documents: Parsed compilation unit.
    :returns: Semantic analysis, including any errors.
    """
    diagnostics: list[Diagnostic] = []
    return analyze_units(resolve_scopes(documents, diagnostics), diagnostics)


def check(documents: Sequence[Document]) -> list[Diagnostic]:
    """Preserve the diagnostic-only checking interface.

    :param documents: Parsed compilation unit.
    :returns: Ordered errors and warnings.
    """
    return list(analyze(documents).diagnostics)
