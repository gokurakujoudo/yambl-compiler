"""Check attribute extensions and single-inheritance field membership."""

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as data_field

from lark import Token, Tree

from yambl_compiler.languages.yambl.semantics.expressions import Resolver
from yambl_compiler.languages.yambl.semantics.fields import field_name
from yambl_compiler.languages.yambl.semantics.signatures import field_signature
from yambl_compiler.languages.yambl.semantics.symbols import (
    EntityKey,
    Unit,
    declaration_name,
    header,
    symbol_key,
)
from yambl_compiler.languages.yambl.semantics.types import (
    FieldTypes,
    Resolved,
    accepts,
    common_type,
)
from yambl_compiler.languages.yambl.syntax.parser import (
    Diagnostic,
    annotations,
    children,
    diagnostic,
    tokens,
)


@dataclass(frozen=True)
class FieldSite:
    """Retain a field's location after merging extensions.

    :param unit: Source and scope of the field declaration.
    :param declaration: Attribute syntax, including metadata and source positions.
    """

    unit: Unit
    declaration: Tree[Token]


@dataclass
class EntityIndex:
    """Hold resolved field relationships for one compilation unit.

    :param fields: Local user fields, including extensions.
    :param ancestors: Ordered nearest-first ancestor keys.
    :param effective: Effective fields, including built-in models.
    :param identities: Logical field identity for each local declaration.
    :param contracts: Public overload contracts by logical identity.
    """

    fields: dict[EntityKey, dict[str, FieldSite]]
    ancestors: dict[EntityKey, list[EntityKey]]
    effective: dict[EntityKey, dict[str, FieldSite]]
    identities: dict[tuple[EntityKey, str], tuple[EntityKey, str]]

    contracts: dict[tuple[EntityKey, str], Resolved] = data_field(
        default_factory=dict[tuple[EntityKey, str], Resolved]
    )

    def field_types(self, key: EntityKey) -> FieldTypes:
        """Collect types in their original lexical scopes.

        :param key: Enclosing entity.
        :returns: Effective field types for self references.
        """
        return {
            name: (site.unit, children(children(site.declaration, 'field_body')[0], 'type_expr')[0])
            for name, site in self.effective.get(key, {}).items()
        }

    def candidates(self, key: EntityKey, mode: str) -> dict[str, list[FieldSite]]:
        """Collect mapping candidates, retaining each branch implementation.

        :param key: Selected entity type.
        :param mode: Attribute category filter.
        :returns: Logical field names mapped to distinct implementation sites.
        """
        result: dict[str, list[FieldSite]] = {}
        for target, fields in self.effective.items():
            if target != key and key not in self.ancestors.get(target, []):
                continue
            for name, site in fields.items():
                syntax = children(children(site.declaration, 'field_body')[0], 'type_expr')[0]
                resolved = Resolver(site.unit, [], self.field_types(target), True).resolve(syntax)
                if str(tokens(children(site.declaration, 'field_body')[0])[0]).replace(
                    'attr', 'static'
                ) == mode and resolved.kind not in ('collection', 'unknown'):
                    implementations = result.setdefault(name, [])
                    if site not in implementations:
                        implementations.append(site)
        return result


def marker(site: FieldSite) -> str:
    """Read a field's relationship marker.

    :param site: Field declaration site.
    :returns: Marker name, or an empty string.
    """
    markers = annotations(site.declaration, 'overload', 'override')
    return str(tokens(markers[0])[1]) if markers else ''


def check_entities(units: Sequence[Unit], diagnostics: list[Diagnostic]) -> EntityIndex:
    """Merge fields and validate final, inheritance, overload and override rules.

    :param units: Parsed sources with file-local scopes.
    :param diagnostics: Destination for relationship diagnostics.
    :returns: Effective fields and logical identities for later type checking.
    """
    parents: dict[EntityKey, EntityKey] = {}
    locations: dict[EntityKey, tuple[Unit, Tree[Token]]] = {}
    fields: dict[EntityKey, dict[str, FieldSite]] = {}
    finals = {
        key
        for unit in units
        for key, (_, node) in unit.world.declarations.items()
        if node.data == 'entity' and tokens(header(node))[0].type == 'FINAL'
    }
    for unit in units:
        for entity in children(unit.document.tree, 'entity'):
            key = symbol_key(unit.document, declaration_name(entity))
            locations[key] = (unit, header(entity))
            fields.setdefault(key, {})
            if tokens(header(entity))[0].type == 'FINAL':
                finals.add(key)
    for unit in units:
        for entity in children(unit.document.tree, 'entity'):
            key = symbol_key(unit.document, declaration_name(entity))
            for inheritance in children(header(entity), 'inheritance'):
                name = str(tokens(inheritance)[1])
                symbol = unit.scope.get(name)
                message = ''
                if name == 'Entity':
                    continue
                if symbol is not None and symbol.key in finals:
                    message = f'cannot inherit final entity: {name}'
                elif symbol is None or symbol.kind != 'entity':
                    message = f'inherits requires a visible entity type: {name}'
                else:
                    parents[key] = symbol.key
                if message:
                    diagnostics.append(diagnostic(unit.document, inheritance, message))
        for node in [
            *children(unit.document.tree, 'entity'),
            *children(unit.document.tree, 'extension'),
        ]:
            name = declaration_name(node)
            symbol = unit.scope.get(name)
            if node.data == 'extension' and symbol is not None and symbol.key in finals:
                diagnostics.append(
                    diagnostic(unit.document, header(node), f'cannot extend final entity: {name}')
                )
                continue
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
    ancestors: dict[EntityKey, list[EntityKey]] = {}
    for key in fields:
        chain: list[EntityKey] = []
        parent = parents.get(key)
        while parent is not None:
            if parent == key or parent in chain:
                unit, declaration = locations[key]
                diagnostics.append(diagnostic(unit.document, declaration, 'inheritance cycle'))
                break
            chain.append(parent)
            parent = parents.get(parent)
        ancestors[key] = chain
    effective: dict[EntityKey, dict[str, FieldSite]] = {}
    for key, local in fields.items():
        merged: dict[str, FieldSite] = {}
        for ancestor in reversed(ancestors[key]):
            merged.update(fields[ancestor])
        merged.update(local)
        effective[key] = merged
    for unit in units:
        unit.world.parents.update(ancestors)
        for key, (scope, declaration) in unit.world.declarations.items():
            if declaration.data in ('entity', 'value'):
                builtin_fields = {
                    field_name(children(f, 'field_body')[0]): FieldSite(scope, f)
                    for f in children(declaration, 'field')
                }
                effective.setdefault(key, builtin_fields)
        unit.world.fields.update(
            {
                key: {name: (site.unit, site.declaration) for name, site in members.items()}
                for key, members in effective.items()
            }
        )
    index = EntityIndex(fields, ancestors, effective, {})
    groups: dict[tuple[EntityKey, str], list[tuple[EntityKey, FieldSite]]] = {}
    for key in sorted(fields, key=lambda item: len(ancestors[item])):
        for name, site in fields[key].items():
            body = children(site.declaration, 'field_body')[0]
            inherited = next(
                (ancestor for ancestor in ancestors[key] if name in fields[ancestor]), None
            )
            root = ancestors[key][-1] if ancestors[key] else key
            identity = (root, name)
            signature = field_signature(site.declaration, site.unit, index.field_types(key), False)
            actual = Resolver(site.unit, [], index.field_types(key), True).resolve(
                children(body, 'type_expr')[0]
            )
            if inherited is not None:
                prior = fields[inherited][name]
                if marker(site) != 'override':
                    diagnostics.append(
                        diagnostic(
                            site.unit.document,
                            body,
                            f'duplicate inherited field: {name}; use @override',
                        )
                    )
                elif signature != field_signature(
                    prior.declaration, prior.unit, index.field_types(inherited), False
                ) or not override_type(actual, prior, index.field_types(inherited)):
                    diagnostics.append(
                        diagnostic(site.unit.document, body, f'override signature mismatch: {name}')
                    )
                else:
                    identity = index.identities.get((inherited, name), (inherited, name))
            elif marker(site) == 'override':
                diagnostics.append(
                    diagnostic(
                        site.unit.document, body, f'override requires an inherited field: {name}'
                    )
                )
            elif str(tokens(body)[0]) != 'id':
                root = ancestors[key][-1] if ancestors[key] else key
                groups.setdefault((root, name), []).append((key, site))
            index.identities[(key, name)] = identity
    replacements: dict[tuple[EntityKey, str], tuple[EntityKey, str]] = {}
    for (root, name), sites in groups.items():
        if len(sites) == 1:
            _, site = sites[0]
            if marker(site) == 'overload':
                diagnostics.append(
                    diagnostic(
                        site.unit.document,
                        site.declaration,
                        f'overload requires another branch implementation: {name}',
                    )
                )
            continue
        signatures = {
            field_signature(site.declaration, site.unit, index.field_types(key), False)
            for key, site in sites
        }
        types = [
            Resolver(site.unit, [], index.field_types(key), True).resolve(
                children(children(site.declaration, 'field_body')[0], 'type_expr')[0]
            )
            for key, site in sites
        ]
        common = common_type(types, sites[0][1].unit.world)
        valid = all(marker(site) == 'overload' for _, site in sites) and len(signatures) == 1
        if valid and common is not None:
            identity = (root, name)
            index.contracts[identity] = common
            for key, _ in sites:
                replacements[(key, name)] = identity
        else:
            for _, site in sites:
                diagnostics.append(
                    diagnostic(
                        site.unit.document,
                        children(site.declaration, 'field_body')[0],
                        f'overload requires marked branches with identical signatures: {name}',
                    )
                )
    for field_key, identity in index.identities.items():
        index.identities[field_key] = replacements.get(identity, identity)
    return index


def override_type(actual: Resolved, prior: FieldSite, self_fields: FieldTypes) -> bool:
    """Check an override against its nearest inherited declaration.

    :param actual: Child field type.
    :param prior: Nearest inherited field.
    :param self_fields: Parent instance context.
    :returns: Whether the type is unchanged or validly narrowed to an exact value.
    """
    expected = Resolver(prior.unit, [], self_fields, True).resolve(
        children(children(prior.declaration, 'field_body')[0], 'type_expr')[0]
    )
    return actual == expected or (
        actual.exact is not None and accepts(expected, actual, prior.unit.world)
    )
