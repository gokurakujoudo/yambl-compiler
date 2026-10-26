"""Dispatch complete data documents to their language parser."""

from lark import Token, Tree

from ..yaplan.model import Plan
from ..yaplan.syntax import from_tree as plan_from_tree
from ..yatran.model import Transaction
from ..yatran.syntax import from_tree as transaction_from_tree
from .syntax import kind, tokenize, tree


def document_tree(text: str) -> Tree[Token]:
    """Select a grammar from the document's leading keyword.

    :param text: Source including optional leading comments.
    :returns: Concrete document tree.
    :raises ValueError: On invalid grammar.
    """
    first = next(t for t in tokenize(text) if t.kind != 'comment')
    return tree(text, 'yaplan' if first.text == 'plan' else 'yatran')


def parse(text: str) -> Plan | Transaction:
    """Read one model-bound data document.

    :param text: Complete Plan or Transaction source.
    :returns: Validated document contract.
    :raises ValueError: On syntax, metadata or integrity errors.
    """
    node = document_tree(text)
    return plan_from_tree(node) if kind(node) == 'plan' else transaction_from_tree(node)
