"""Build and verify execution records using the YATRAN Lark grammar."""

import re
from datetime import datetime
from uuid import UUID

from lark import Token, Tree

from ..shared.syntax import (
    children,
    columns,
    digest,
    fail,
    revision,
    rows,
    scope,
    string,
    tokens,
    tree,
)
from ..yaplan.syntax import from_tree as plan_from_tree
from .model import Change, Image, Transaction, TransactionGroup


def image(node: Tree[Token]) -> Image:
    """Decode an exact before or after image.

    :param node: Image subtree.
    :returns: Typed business image, including absence.
    """
    names = columns(children(node, 'columns')[0])
    return Image(
        str(tokens(children(node, 'target')[0])[0]),
        names,
        rows(children(node, 'rows')[0], len(names)),
    )


def from_tree(node: Tree[Token]) -> Transaction:
    """Decode an execution envelope and validate its integrity.

    :param node: Complete transaction tree.
    :returns: Verified transaction.
    :raises ValueError: On metadata, group, image or digest errors.
    """
    from .writing import semantic_digest

    id_ = string(tokens(node)[1])
    version = revision(children(node, 'revision')[0])
    time = string(tokens(children(node, 'time')[0])[1])
    sources = children(node, 'source')
    source = string(tokens(sources[0])[1]) if sources else ''
    try:
        if str(UUID(id_)) != id_ or (source and str(UUID(source)) != source):
            raise ValueError('noncanonical UUID')
        if not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z', time
        ):
            raise ValueError('timestamp must use YYYY-MM-DDTHH:MM:SS[.fraction]Z')
        datetime.fromisoformat(time)
    except ValueError as error:
        raise fail(node, f'invalid transaction metadata: {error}') from error
    checksum = digest(children(node, 'digest')[0])
    plan = plan_from_tree(children(node, 'plan')[0])
    if plan.model is None:
        raise fail(node, 'transaction plan requires a model digest')
    groups: list[TransactionGroup] = []
    for group in children(node, 'transaction_group'):
        name = str(tokens(group)[1])
        if name not in {g.name for g in plan.groups} or name in {g.name for g in groups}:
            raise fail(group, 'unknown or duplicate transaction group')
        changes: list[Change] = []
        for change in children(group, 'change'):
            before = tuple(image(item) for item in children(change, 'before'))
            after = tuple(image(item) for item in children(change, 'after'))
            if not before and not after:
                raise fail(change, 'change requires a before or after image')
            changes.append(
                Change(
                    str(tokens(children(change, 'target')[0])[0]),
                    scope(children(change, 'scope')[0]),
                    before,
                    after,
                )
            )
        warnings = tuple(string(tokens(warning)[1]) for warning in children(group, 'warning'))
        groups.append(TransactionGroup(name, tuple(changes), warnings))
    result = Transaction(id_, time, plan, tuple(groups), source, checksum, version)
    try:
        actual = semantic_digest(result)
    except (MemoryError, OverflowError, ValueError) as error:
        raise fail(node, 'numeric representation exceeds runtime limits') from error
    if checksum != actual:
        raise fail(node, 'transaction digest mismatch')
    return result


def parse(text: str) -> Transaction:
    """Parse and verify a complete YATRAN document.

    :param text: Transaction source.
    :returns: Typed execution record.
    :raises ValueError: On invalid syntax, metadata or integrity.
    """
    return from_tree(tree(text, 'yatran'))
