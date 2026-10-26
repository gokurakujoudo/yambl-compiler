"""Build exact data documents for language and runtime tests."""

from decimal import Decimal

from yambl_compiler.languages.shared.model import Condition
from yambl_compiler.languages.yaplan.model import Group, Plan
from yambl_compiler.languages.yatran.model import Change, Image, Transaction, TransactionGroup
from yambl_compiler.languages.yatran.writing import seal

DIGEST = 'a' * 64


def source(body='', metadata=None):
    return (
        "plan daily {\n"
        + (metadata or f"version 1\nmodel '{DIGEST}'")
        + "\ngroup first {\n"
        + body
        + "\n}\n}\n"
    )


def transaction():
    plan = Plan('daily', DIGEST, (Group('first', ()),))
    before = Image('core.Security', ('security_id', 'coupon'), (('S1', Decimal('1.25')),))
    after = Image('core.Security', ('security_id', 'coupon'), (('S1', Decimal('2.50')),))
    return seal(
        Transaction(
            '12345678-1234-1234-1234-123456789abc',
            '2026-10-26T12:34:56Z',
            plan,
            (
                TransactionGroup(
                    'first',
                    (
                        Change(
                            'core.Security.coupon',
                            (Condition('security_id', 'S1'),),
                            (before,),
                            (after,),
                        ),
                    ),
                    ('later change retained',),
                ),
            ),
        )
    )
