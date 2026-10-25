"""Collect entity-scoped dimensions and check their field applications."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from lark import Token, Tree

from yambl_compiler.computation.records import Expression
from yambl_compiler.semantics.expressions import Resolver
from yambl_compiler.semantics.symbols import (
    EntityKey,
    Unit,
    declaration_name,
    field_owner,
)
from yambl_compiler.semantics.types import Resolved
from yambl_compiler.syntax.computation import branches, expression
from yambl_compiler.syntax.parser import (
    Diagnostic,
    annotations,
    children,
    diagnostic,
    tokens,
)


@dataclass(frozen=True)
class Dimension:
    """Retain a resolved dimension's names and instance-dependent selection policy.

    :param owner: Entity declaring the dimension.
    :param name: Local declaration name.
    :param unit: Original lexical scope.
    :param node: Declaration syntax.
    :param value_name: Explicit value parameter name.
    :param value_type: String-compatible dimension type.
    :param hierarchy_name: Selector parameter name, empty if absent.
    :param hierarchy_default: Optional selector reference expression.
    :param fallback: Optional per-instance snapshot look-back expression.
    """

    owner: EntityKey
    name: str
    unit: Unit
    node: Tree[Token]
    value_name: str
    value_type: Resolved
    hierarchy_name: str
    hierarchy_default: Expression | None
    fallback: Expression | None


def collect_dimensions(units: Sequence[Unit], diagnostics: list[Diagnostic]) -> None:
    """Collect declarations before field relationships bind metadata.

    :param units: Resolved source scopes.
    :param diagnostics: Diagnostic destination.
    :returns: None; populates each compilation world's dimension index.
    """
    for unit in units:
        for declaration in [
            *children(unit.document.tree, "entity"),
            *children(unit.document.tree, "extension"),
        ]:
            owner = unit.scope.get(declaration_name(declaration))
            if owner is None or owner.kind != "entity":
                continue
            target = unit.world.dimensions.setdefault(owner.key, {})
            for node in children(declaration, "dimension"):
                name = str(tokens(node)[1])
                if name in target:
                    diagnostics.append(
                        diagnostic(
                            unit.document,
                            node,
                            f"duplicate dimension declaration: {name}",
                        )
                    )
                for role in [
                    node,
                    *children(node, "dimension_value"),
                    *children(node, "dimension_hierarchy"),
                ]:
                    if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", str(tokens(role)[1])):
                        diagnostics.append(
                            diagnostic(
                                unit.document,
                                role,
                                "dimension names must use snake_case",
                            )
                        )
                target[name] = (unit, node)


def resolve_dimension(
    unit: Unit, owner: EntityKey, name: str, diagnostics: list[Diagnostic]
) -> Dimension | None:
    """Find an inherited dimension and resolve its original lexical types.

    :param unit: Use-site compilation world.
    :param owner: Entity owning the decorated field.
    :param name: Dimension name.
    :param diagnostics: Diagnostic destination.
    :returns: Resolved declaration, or None when absent.
    """
    for key in [owner, *unit.world.parents.get(owner, [])]:
        site = unit.world.dimensions.get(key, {}).get(name)
        if site is None:
            continue
        scope, node = site
        value = children(node, "dimension_value")[0]
        hierarchy = children(node, "dimension_hierarchy")
        fallback = children(node, "dimension_fallback")
        resolver = Resolver(scope, diagnostics, {})
        value_type = resolver.resolve(children(value, "type_expr")[0])
        hierarchy_default = None
        if hierarchy:
            selector = resolver.resolve(children(hierarchy[0], "type_expr")[0])
            if selector.key != scope.scope["Hierarchy"].key:
                diagnostics.append(
                    diagnostic(
                        scope.document,
                        hierarchy[0],
                        "dimension hierarchy requires Hierarchy",
                    )
                )
            defaults = [part for part in branches(hierarchy[0]) if part.data != "type_expr"]
            if defaults:
                hierarchy_default = expression(defaults[0])
                if hierarchy_default.kind != "string":
                    diagnostics.append(
                        diagnostic(
                            scope.document,
                            hierarchy[0],
                            "hierarchy default requires a string reference ID",
                        )
                    )
        if value_type.name not in ("string", "code"):
            diagnostics.append(
                diagnostic(scope.document, value, "dimension value must be string or code")
            )
        return Dimension(
            key,
            name,
            scope,
            node,
            str(tokens(value)[1]),
            value_type,
            str(tokens(hierarchy[0])[1]) if hierarchy else "",
            hierarchy_default,
            expression(branches(fallback[0])[0]) if fallback else None,
        )
    return None


def field_dimension(
    unit: Unit, field: Tree[Token], call: Tree[Token], diagnostics: list[Diagnostic]
) -> Dimension | None:
    """Resolve one named dimension invocation on a field.

    :param unit: Field lexical scope.
    :param field: Decorated field syntax.
    :param call: Dimension decorator call.
    :param diagnostics: Diagnostic destination.
    :returns: Resolved dimension, or None after a diagnostic.
    """
    if children(call, "pair_argument"):
        diagnostics.append(
            diagnostic(
                unit.document,
                call,
                "inline @dimension is no longer supported; declare dimension name { ... } and use @dimension(name)",
            )
        )
        return None
    arguments = [
        item for item in branches(call) if item.data in ("positional_argument", "keyword_argument")
    ]
    bodies = children(field, "field_body")
    if len(arguments) != 1 or not bodies:
        diagnostics.append(
            diagnostic(
                unit.document,
                call,
                "@dimension requires one named dimension reference on a field",
            )
        )
        return None
    syntax = children(bodies[0], "type_expr")[0]
    owner = field_owner(unit, syntax)
    reference = children(arguments[0], "type_expr")[0]
    items = tokens(reference)
    name = str(items[0])
    found = (
        resolve_dimension(unit, owner.key, name, diagnostics)
        if owner and len(items) == 1 and items[0].type == "NAME"
        else None
    )
    if found is None:
        diagnostics.append(diagnostic(unit.document, reference, f"unknown dimension: {name}"))
    return found


def check_dimensions(units: Sequence[Unit], diagnostics: list[Diagnostic]) -> None:
    """Validate declarations, inherited names and temporal compatibility.

    :param units: Complete checked scopes.
    :param diagnostics: Diagnostic destination.
    :returns: None.
    """
    visited: set[tuple[EntityKey, str]] = set()
    for unit in units:
        for owner, definitions in unit.world.dimensions.items():
            for name, (scope, node) in definitions.items():
                if (owner, name) in visited:
                    continue
                visited.add((owner, name))
                inherited = any(
                    name in unit.world.dimensions.get(parent, {})
                    for parent in unit.world.parents.get(owner, [])
                )
                if inherited or name in unit.world.fields.get(owner, {}):
                    diagnostics.append(
                        diagnostic(
                            scope.document,
                            node,
                            f"conflicting dimension declaration: {name}",
                        )
                    )
                resolve_dimension(unit, owner, name, diagnostics)
        for declaration in [
            *children(unit.document.tree, "entity"),
            *children(unit.document.tree, "extension"),
        ]:
            for field in children(declaration, "field"):
                used: set[str] = set()
                for call in annotations(field, "temporal"):
                    used.update(str(tokens(p)[0]) for p in children(call, "pair_argument"))
                for call in annotations(field, "dimension"):
                    found = field_dimension(unit, field, call, diagnostics)
                    if found is None:
                        continue
                    mode = str(tokens(children(field, "field_body")[0])[0])
                    if found.fallback is not None and mode != "snapshot":
                        diagnostics.append(
                            diagnostic(
                                unit.document,
                                call,
                                "fallback_days is supported only on snapshot fields",
                            )
                        )
                    for key in filter(None, (found.value_name, found.hierarchy_name)):
                        if key in used:
                            diagnostics.append(
                                diagnostic(unit.document, call, f"duplicate dimension: {key}")
                            )
                        used.add(key)
