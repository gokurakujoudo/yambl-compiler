"""Validate finite structured values and their field restrictions."""

from collections.abc import Sequence

from lark import Token, Tree

from yambl_compiler.semantics.fields import check_field, check_metadata, field_name
from yambl_compiler.semantics.symbols import EntityKey, Unit, declaration_name, symbol_key
from yambl_compiler.syntax.parser import Diagnostic, children, diagnostic, tokens


def check_values(units: Sequence[Unit], diagnostics: list[Diagnostic]) -> None:
    """Check value fields and reject recursive value containment.

    :param units: Resolved compilation units.
    :param diagnostics: Destination for static errors.
    :returns: None; appends diagnostics.
    """
    edges: dict[EntityKey, set[EntityKey]] = {}
    sites: dict[EntityKey, tuple[Unit, Tree[Token]]] = {}
    for unit in units:
        for value in children(unit.document.tree, 'value'):
            key = symbol_key(unit.document, declaration_name(value))
            sites[key] = (unit, value)
            edges[key] = set()
            check_metadata(value, unit, diagnostics)
            names: set[str] = set()
            for field in children(value, 'field'):
                check_field(field, unit, diagnostics, False)
                body = children(field, 'field_body')[0]
                name = field_name(body)
                if name in names:
                    diagnostics.append(diagnostic(unit.document, body, f'duplicate field: {name}'))
                names.add(name)
                if str(tokens(body)[0]) not in ('attr', 'static'):
                    diagnostics.append(
                        diagnostic(unit.document, body, 'value fields must be attr or static attr')
                    )
                for kind in ('temporal', 'dimension_annotation', 'field_marker'):
                    for annotation in children(field, kind):
                        diagnostics.append(
                            diagnostic(
                                unit.document,
                                annotation,
                                'value fields allow only title and description annotations',
                            )
                        )
                node = children(body, 'type_expr')[0]
                type_name = str(tokens(node)[0])
                if type_name in ('collection', 'values', 'map', 'field_mapping'):
                    diagnostics.append(
                        diagnostic(unit.document, node, 'value fields cannot be collections')
                    )
                target = unit.scope.get(type_name)
                if target is not None and target.kind == 'value':
                    edges[key].add(target.key)
    for key in edges:
        pending = list(edges[key])
        seen: set[EntityKey] = set()
        while pending:
            next_key = pending.pop()
            if next_key == key:
                unit, value = sites[key]
                diagnostics.append(diagnostic(unit.document, value, 'value containment cycle'))
                break
            if next_key not in seen:
                seen.add(next_key)
                pending.extend(edges.get(next_key, ()))
