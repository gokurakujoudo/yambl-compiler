"""Build Plan contracts from the YAPLAN Lark grammar."""

from typing import Literal, cast

from lark import Token, Tree

from ..shared.syntax import children, columns, digest, fail, revision, rows, scope, tokens, tree
from .model import Group, Operation, Plan


def operation(node: Tree[Token]) -> Operation:
    """Bind one syntactic operation to its data contract.

    :param node: Operation subtree.
    :returns: Shape-checked operation.
    :raises ValueError: On incompatible operation clauses.
    """
    first = tokens(node)[0]
    kind = cast(Literal['insert', 'update', 'delete', 'replace'], str(first))
    target = str(tokens(children(node, 'target')[0])[0])
    expectations = children(node, 'expectation')
    expect = cast(Literal['one', 'any'], str(tokens(expectations[0])[1])) if expectations else None
    if expect and kind not in ('replace', 'delete'):
        raise fail(node, 'expect is only valid for replace or delete')
    owned = bool(children(node, 'owned'))
    scopes = children(node, 'scope')
    if owned and kind != 'delete':
        raise fail(node, 'owned_data is only valid for entity deletion')
    if scopes and (kind not in ('replace', 'delete') or owned):
        raise fail(node, 'scope requires replace or field delete')
    if kind == 'replace' and not scopes:
        raise fail(node, 'replace requires scope')
    conditions = scope(scopes[0]) if scopes else ()
    headings = children(node, 'columns')
    if conditions and kind == 'delete':
        if headings:
            raise fail(node, 'field delete cannot contain data rows')
        return Operation(kind, target, (), (), conditions, line=first.line or 1, expect=expect)
    if not headings:
        raise fail(node, 'replace requires columns and data rows')
    names = columns(headings[0])
    return Operation(
        kind,
        target,
        names,
        rows(children(node, 'rows')[0], len(names)),
        conditions,
        owned,
        first.line or 1,
        expect,
    )


def from_tree(node: Tree[Token]) -> Plan:
    """Decode a standalone or embedded Plan.

    :param node: Complete Plan subtree.
    :returns: Model-bound plan.
    :raises ValueError: On invalid metadata or repeated groups.
    """
    version = revision(children(node, 'revision')[0])
    model = digest(children(node, 'model_digest')[0])
    groups: list[Group] = []
    for group in children(node, 'group'):
        name = str(tokens(group)[1])
        if name in {g.name for g in groups}:
            raise fail(group, f'duplicate group: {name}')
        groups.append(Group(name, tuple(operation(op) for op in children(group, 'operation'))))
    return Plan(str(tokens(node)[1]), model, tuple(groups), version)


def parse(text: str) -> Plan:
    """Parse one complete YAPLAN document.

    :param text: Plan source.
    :returns: Typed plan.
    :raises ValueError: On invalid syntax or metadata.
    """
    return from_tree(tree(text, 'yaplan'))
