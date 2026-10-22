"""Check attribute extensions and single-inheritance field membership."""

from collections.abc import Sequence
from dataclasses import dataclass

from lark import Token, Tree

from yambl_compiler.semantics.fields import field_name
from yambl_compiler.semantics.symbols import ENTITIES, EntityKey, Unit, header, namespace
from yambl_compiler.syntax.parser import Diagnostic, children, diagnostic, tokens


@dataclass(frozen=True)
class FieldSite:
    """Retain a field's location after merging extensions.

    :param unit: Source and scope of the field declaration.
    :param declaration: Attribute syntax, including metadata and source positions.
    """

    unit: Unit
    declaration: Tree[Token]


def check_entities(
    units: Sequence[Unit], diagnostics: list[Diagnostic]
) -> dict[EntityKey, dict[str, FieldSite]]:
    """Validate extensions and inherited fields after all declarations are known.

    :param units: Parsed sources with file-local name resolution.
    :param diagnostics: Destination for target, cycle, and duplicate-field errors.
    :returns: Local fields merged into their entity, retaining each field's source.
        Extensions have no separate identity. Inherited fields are checked against
        the merged definitions; entity instances are not evaluated.
    """
    parents: dict[EntityKey, EntityKey] = {}
    locations: dict[EntityKey, tuple[Unit, Tree[Token]]] = {}
    fields: dict[EntityKey, dict[str, FieldSite]] = {}
    for unit in units:
        for entity in children(unit.document.tree, 'entity'):
            declaration = header(entity)
            key = (namespace(unit.document), str(tokens(declaration)[1]))
            locations[key] = (unit, declaration)
            fields.setdefault(key, {})
            for inheritance in children(declaration, 'inheritance'):
                name = str(tokens(inheritance)[1])
                symbol = unit.scope.get(name)
                if name in ENTITIES:
                    diagnostics.append(
                        diagnostic(
                            unit.document,
                            inheritance,
                            'built-in inheritance is unsupported; Decodes is sealed',
                        )
                    )
                elif symbol is None or symbol.kind != 'entity':
                    diagnostics.append(
                        diagnostic(
                            unit.document,
                            inheritance,
                            f'inherits requires a visible entity type: {name}',
                        )
                    )
                else:
                    parents[key] = symbol.key
    for unit in units:
        for node in [
            *children(unit.document.tree, 'entity'),
            *children(unit.document.tree, 'extension'),
        ]:
            name = str(tokens(header(node))[1])
            symbol = unit.scope.get(name)
            if symbol is None or symbol.kind != 'entity':
                diagnostics.append(
                    diagnostic(
                        unit.document,
                        header(node),
                        f'extend requires a visible user-defined entity: {name}',
                    )
                )
                continue
            target = fields[symbol.key]
            for field in children(node, 'field'):
                body = children(field, 'field_body')[0]
                name = field_name(body)
                if name in target:
                    diagnostics.append(diagnostic(unit.document, body, f'duplicate field: {name}'))
                target[name] = FieldSite(unit, field)
    for key, local in fields.items():
        seen = {key}
        inherited: set[str] = set()
        parent = parents.get(key)
        while parent is not None:
            if parent in seen:
                unit, declaration = locations[key]
                diagnostics.append(diagnostic(unit.document, declaration, 'inheritance cycle'))
                break
            seen.add(parent)
            inherited.update(fields[parent])
            parent = parents.get(parent)
        for name in local.keys() & inherited:
            site = local[name]
            diagnostics.append(
                diagnostic(
                    site.unit.document,
                    children(site.declaration, 'field_body')[0],
                    f'duplicate inherited field: {name}',
                )
            )
    return fields
