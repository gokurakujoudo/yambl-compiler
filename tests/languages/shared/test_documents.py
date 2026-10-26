"""Exercise actual plan records, syntax failures and sealed transaction images."""

import json
import unittest
from decimal import Decimal
from pathlib import Path

from tests.languages.support import DIGEST
from yambl_compiler.languages.shared.model import ObjectValue
from yambl_compiler.languages.shared.writing import literal
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.transactions.formatting import format_document
from yambl_compiler.transactions.syntax import DocumentError, parse, tokenize
from yambl_compiler.transactions.writing import dumps


class DocumentTests(unittest.TestCase):
    def test_shared_editor_cases(self):
        fixture = (
            Path(__file__).resolve().parents[4]
            / 'yambl/tools/vscode/lsp/tests/transaction-cases.json'
        )
        for case in json.loads(fixture.read_text(encoding='utf-8')):
            with self.subTest(case=case['name']):
                if case.get('invalid'):
                    with self.assertRaises(DocumentError):
                        parse(case['text'])
                else:
                    parse(case['text'])
                    self.assertEqual(format_document(case['text']), case['formatted'])
                    self.assertEqual(format_document(case['formatted']), case['formatted'])

    def test_error_coordinates(self):
        with self.assertRaises(DocumentError) as caught:
            tokenize('plan x {\n  @')
        self.assertEqual((caught.exception.line, caught.exception.column), (2, 3))
        self.assertIn('2:3:', str(caught.exception))

    def test_literal_writing(self):
        cases = [
            (Decimal('-0.000'), '0.0'),
            (Decimal('100'), '100.0'),
            (Decimal('1.25'), '1.25'),
            ((), '[]'),
            (ObjectValue(()), '{}'),
            (True, 'true'),
            (False, 'false'),
            (123, '123'),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(literal(value), expected)
        for value in ('line\nbreak', 'line\rbreak', Decimal('NaN'), Decimal('Infinity')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                literal(value)
        with self.assertRaisesRegex(ValueError, 'owned_data'):
            dumps(
                Plan(
                    'p',
                    DIGEST,
                    (
                        Group(
                            'g',
                            (Operation('insert', 'core.A', ('id',), (('x',),), owned_data=True),),
                        ),
                    ),
                )
            )
