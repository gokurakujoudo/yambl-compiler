"""Serialize and validate complete data documents before publication."""

from yambl_compiler.languages.yaplan.model import Plan
from yambl_compiler.languages.yaplan.writing import plan_lines
from yambl_compiler.languages.yatran.model import Transaction
from yambl_compiler.languages.yatran.writing import transaction_lines


def dumps(document: Plan | Transaction) -> str:
    """Serialize and validate one document before publication.

    :param document: Plan or execution record.
    :returns: UTF-8-ready text with one final LF.
    :raises ValueError: On an invalid document or stale transaction digest.
    """
    from yambl_compiler.languages.shared.documents import parse

    lines = (
        plan_lines(document)
        if isinstance(document, Plan)
        else transaction_lines(document, document.digest)
    )
    text = '\n'.join(lines) + '\n'
    if parse(text) != document:
        raise ValueError('document does not round-trip through the data grammar')
    return text
