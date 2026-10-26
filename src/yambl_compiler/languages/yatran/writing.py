"""Render and seal execution records."""

import hashlib
from dataclasses import replace

from ..shared.writing import data_lines, literal, scope_text
from ..yaplan.writing import plan_lines
from .model import Transaction


def transaction_lines(transaction: Transaction, digest: str) -> list[str]:
    """Render an execution record with the supplied digest.

    :param transaction: Execution data.
    :param digest: Digest to write in the envelope.
    :returns: Transaction lines.
    """
    lines = [
        'transaction ' + literal(transaction.id) + ' {',
        '    version ' + str(transaction.version),
        '    time ' + literal(transaction.time),
    ]
    if transaction.source:
        lines.append('    source ' + literal(transaction.source))
    lines.append('    digest ' + literal(digest))
    lines.extend(plan_lines(transaction.plan, 1))
    for group in transaction.groups:
        lines.append('    group ' + group.name + ' {')
        for change in group.changes:
            lines.append('        change ' + change.target + ' ' + scope_text(change.scope) + ' {')
            for label, images in (('before', change.before), ('after', change.after)):
                for image in images:
                    lines.extend(
                        data_lines(label + ' ' + image.target, image.columns, image.rows, 3)
                    )
            lines.append('        }')
        lines.extend('        warning ' + literal(w) for w in group.warnings)
        lines.append('    }')
    lines.append('}')
    return lines


def semantic_digest(transaction: Transaction) -> str:
    """Hash canonical data, excluding the digest itself and source formatting.

    :param transaction: Execution record.
    :returns: Lowercase SHA-256.
    """
    return hashlib.sha256(
        ('\n'.join(transaction_lines(transaction, '')) + '\n').encode()
    ).hexdigest()


def seal(transaction: Transaction) -> Transaction:
    """Fill an execution record's integrity digest.

    :param transaction: Record before publication.
    :returns: Immutable sealed record.
    """
    return replace(transaction, digest=semantic_digest(transaction))
