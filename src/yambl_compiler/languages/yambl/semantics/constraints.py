"""Check entity constraints and their ordered calculation statements."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from lark import Token, Tree

from yambl_compiler.languages.yambl.computation.records import Expression, Span, TypeHint
from yambl_compiler.languages.yambl.semantics.computation import (
    IDENTIFIER,
    Access,
    Checker,
    Computations,
    Value,
)
from yambl_compiler.languages.yambl.semantics.entities import EntityIndex
from yambl_compiler.languages.yambl.semantics.expressions import Resolver
from yambl_compiler.languages.yambl.semantics.symbols import (
    RESERVED_CALCULATION_NAMES,
    EntityKey,
    Unit,
    declaration_name,
)
from yambl_compiler.languages.yambl.semantics.types import Resolved
from yambl_compiler.languages.yambl.syntax.computation import (
    branches,
    expression,
    restore_hint,
    span,
    type_hint,
)
from yambl_compiler.languages.yambl.syntax.parser import Diagnostic, children, diagnostic, tokens


@dataclass(frozen=True)
class Statement:
    """Keep a checked let or assert in its original execution position.

    :param kind: Let binding or assertion.
    :param name: Local name, empty for an assertion.
    :param expression: Checked expression.
    :param annotation: Optional explicit local type.
    :param span: Original statement location.
    :param assert_source: Original assertion text, empty for a let binding.
    """

    kind: str
    name: str
    expression: Expression
    annotation: TypeHint | None
    span: Span
    assert_source: str


@dataclass(frozen=True)
class Constraint:
    """Keep a checked entity constraint in its defining lexical scope.

    :param owner: Declaring entity identity.
    :param name: Local constraint name.
    :param unit: Original lexical scope.
    :param node: Constraint syntax.
    :param extension: Whether the declaration comes from an extend block.
    :param statements: Ordered checked statements.
    :param accesses: Resolved field operations.
    :param dependencies: Referenced computed implementations.
    """

    owner: EntityKey
    name: str
    unit: Unit
    node: Tree[Token]
    extension: bool
    statements: tuple[Statement, ...]
    accesses: tuple[Access, ...]
    dependencies: tuple[tuple[EntityKey, str], ...]


def check_constraints(
    units: Sequence[Unit], entities: EntityIndex, diagnostics: list[Diagnostic]
) -> dict[EntityKey, dict[str, Constraint]]:
    """Check declarations, lexical scopes, statement order, and inherited names.

    :param units: Resolved source and embedded scopes.
    :param entities: Checked entity inheritance and fields.
    :param diagnostics: Destination for source diagnostics.
    :returns: Direct constraints indexed by owner and name.
    """
    result: dict[EntityKey, dict[str, Constraint]] = {}
    for unit in units:
        analyzer = cast(Computations, unit.world.computations)
        for declaration in (
            *children(unit.document.tree, 'entity'),
            *children(unit.document.tree, 'extension'),
        ):
            symbol = unit.scope.get(declaration_name(declaration))
            if symbol is None or symbol.kind != 'entity':
                continue
            owner = symbol.key
            local = result.setdefault(owner, {})
            for node in children(declaration, 'constraint'):
                name = str(tokens(node)[1])
                if name in RESERVED_CALCULATION_NAMES:
                    diagnostics.append(
                        diagnostic(unit.document, node, f'reserved calculation name: {name}')
                    )
                if not IDENTIFIER.fullmatch(name):
                    diagnostics.append(
                        diagnostic(unit.document, node, 'constraint names must use snake_case')
                    )
                if name in local:
                    diagnostics.append(
                        diagnostic(unit.document, node, f'duplicate constraint: {name}')
                    )
                    continue
                checker = Checker(analyzer, owner, unit)
                environment = {'self': Value(Resolved(owner[1], 'reference', key=owner))}
                statements: list[Statement] = []
                for item in branches(node):
                    kind = 'let' if item.data == 'let_statement' else 'assert'
                    item_tokens = tokens(item)
                    local_name = str(item_tokens[1]) if kind == 'let' else ''
                    hint = (
                        type_hint(children(item, 'type_expr')[0])
                        if children(item, 'type_expr')
                        else None
                    )
                    node_expression = expression(branches(item)[-1])
                    declared = (
                        Resolver(unit, diagnostics, entities.field_types(owner)).resolve(
                            restore_hint(hint)
                        )
                        if hint
                        else None
                    )
                    value = checker.read(
                        checker.infer(node_expression, environment, declared), node_expression
                    )
                    if kind == 'let':
                        if (
                            not IDENTIFIER.fullmatch(local_name)
                            or local_name in environment
                            or local_name == 'input'
                        ):
                            checker.issue(
                                node_expression, f'duplicate or invalid local binding: {local_name}'
                            )
                        if declared is not None and not checker.compatible(declared, value.type):
                            checker.issue(
                                node_expression,
                                f'incompatible local binding: expected {declared.name}, got {value.type.name}',
                            )
                        environment[local_name] = Value(declared) if declared else value
                    elif value.type.name != 'bool' and value.type.kind != 'unknown':
                        checker.issue(node_expression, 'constraint assert requires bool')
                    location = span(item)
                    source = (
                        unit.document.text[location.start : location.end]
                        if kind == 'assert'
                        else ''
                    )
                    statements.append(
                        Statement(
                            kind,
                            local_name,
                            checker.elaborate(node_expression),
                            checker.checked_hint(hint) if hint else None,
                            location,
                            source,
                        )
                    )
                if checker.inputs:
                    diagnostics.append(
                        diagnostic(
                            unit.document,
                            node,
                            'constraint cannot have external inputs: '
                            + ', '.join(sorted(checker.inputs)),
                        )
                    )
                local[name] = Constraint(
                    owner,
                    name,
                    unit,
                    node,
                    declaration.data == 'extension',
                    tuple(statements),
                    tuple(checker.accesses),
                    tuple(sorted(checker.dependencies)),
                )
    for owner, local in result.items():
        for name, constraint in local.items():
            if any(name in result.get(parent, {}) for parent in entities.ancestors.get(owner, [])):
                diagnostics.append(
                    diagnostic(
                        constraint.unit.document,
                        constraint.node,
                        f'duplicate inherited constraint: {name}',
                    )
                )
    return result
