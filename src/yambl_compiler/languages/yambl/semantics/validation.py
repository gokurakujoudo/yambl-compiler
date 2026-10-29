"""Coordinate static checks across a complete compilation unit."""

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as data_field

from yambl_compiler.languages.yambl.semantics.computation import Calculation, check_computations
from yambl_compiler.languages.yambl.semantics.decodes import check_decodes
from yambl_compiler.languages.yambl.semantics.decorators import check_decorators
from yambl_compiler.languages.yambl.semantics.dimensions import check_dimensions, collect_dimensions
from yambl_compiler.languages.yambl.semantics.entities import EntityIndex, check_entities
from yambl_compiler.languages.yambl.semantics.expressions import check_aliases
from yambl_compiler.languages.yambl.semantics.fields import check_field
from yambl_compiler.languages.yambl.semantics.hierarchies import hierarchy_entries
from yambl_compiler.languages.yambl.semantics.lookups import Lookup, check_lookups
from yambl_compiler.languages.yambl.semantics.symbols import (
    EntityKey,
    Unit,
    declaration_name,
    model_units,
    resolve_scopes,
)
from yambl_compiler.languages.yambl.semantics.values import check_values
from yambl_compiler.languages.yambl.syntax.parser import Diagnostic, Document, children


@dataclass(frozen=True)
class Analysis:
    """Retain checked scopes and relationships for semantic consumers.

    :param units: Analyzed compilation units.
    :param entities: Effective fields and logical identities.
    :param diagnostics: Ordered errors and warnings.
    :param calculations: Typed computation bodies and inferred parameter contracts.
    :param lookups: Effective named rules and their inferred contracts.
    """

    units: tuple[Unit, ...]
    entities: EntityIndex
    diagnostics: tuple[Diagnostic, ...]
    calculations: dict[tuple[EntityKey, str], Calculation] = data_field(
        default_factory=dict[tuple[EntityKey, str], Calculation]
    )
    lookups: dict[EntityKey, dict[str, Lookup]] = data_field(
        default_factory=dict[EntityKey, dict[str, Lookup]]
    )


def analyze_units(units: list[Unit], diagnostics: list[Diagnostic]) -> Analysis:
    """Validate a compilation unit and report every unresolved Decodes reference.

    :param units: Resolved scopes, including embedded definitions when loading IR.
    :param diagnostics: Existing scope diagnostics.
    :returns: Checked semantic model and source-ordered diagnostics.
    """
    sources = model_units(units)
    collect_dimensions(sources, diagnostics)
    entities = check_entities(sources, diagnostics)
    check_dimensions(sources, diagnostics)
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
        for hierarchy in children(unit.document.tree, 'hierarchy'):
            hierarchy_entries(unit, hierarchy, diagnostics)
    calculations = check_computations(entities, diagnostics, units)
    lookups = check_lookups(sources, entities, diagnostics)
    return Analysis(
        tuple(units),
        entities,
        tuple(
            sorted(
                set(diagnostics),
                key=lambda item: (str(item.path), item.line, item.column, item.message),
            )
        ),
        calculations,
        lookups,
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
