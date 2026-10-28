"""Exercise actual plan records, syntax failures and sealed transaction images."""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.languages.support import DIGEST, transaction
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.shared.formatting import format_document
from yambl_compiler.languages.shared.syntax import DocumentError
from yambl_compiler.languages.yatran.model import Image, Transaction, TransactionGroup
from yambl_compiler.languages.yatran.writing import seal, semantic_digest
from yambl_compiler.transactions.writing import dumps


class DocumentTests(unittest.TestCase):
    def test_lark_entry_point_requires_a_transaction(self):
        from yambl_compiler.languages.yatran.syntax import parse as parse_transaction

        expected = transaction()
        self.assertEqual(parse_transaction(dumps(expected)), expected)
        with self.assertRaises(DocumentError):
            parse_transaction(dumps(expected.plan))

    def test_digest_numeric_expansion_failure_is_a_document_diagnostic(self):
        text = dumps(transaction())
        for error in (MemoryError, OverflowError, ValueError):
            with (
                self.subTest(error=error),
                patch('yambl_compiler.languages.yatran.writing.semantic_digest', side_effect=error),
            ):
                with self.assertRaisesRegex(DocumentError, 'numeric representation'):
                    parse(text)

    def test_shared_sealed_image_cases(self):
        fixture = (
            Path(__file__).resolve().parents[4]
            / 'yambl/tools/vscode/lsp/tests/transaction-image-cases.json'
        )
        for case in json.loads(fixture.read_text(encoding='utf-8'))['cases']:
            with self.subTest(case=case['name']):
                document = parse(case['text'])
                self.assertIsInstance(document, Transaction)
                self.assertEqual(parse(format_document(case['text'])), document)

    def test_transaction_roundtrip_and_integrity(self):
        value = transaction()
        text = dumps(value)
        self.assertEqual(parse(text), value)
        self.assertIn("'S1', 2.5", text)
        self.assertIn("warning 'later change retained'", text)
        self.assertEqual(parse(text.replace('2.5', '2.5000')), value)
        with self.assertRaisesRegex(DocumentError, 'digest mismatch'):
            parse(text.replace('2.5', '2.6'))
        rollback = seal(replace(value, source='87654321-1234-1234-1234-123456789abc'))
        self.assertEqual(parse(dumps(rollback)), rollback)
        self.assertNotEqual(rollback.digest, value.digest)
        self.assertEqual(semantic_digest(replace(value, digest='other')), value.digest)

    def test_transaction_ids_accept_snowflake_and_legacy_uuid(self):
        for id_, source in (
            ('1', '123456789012345678'),
            ('123456789012345678', '87654321-1234-1234-1234-123456789abc'),
            ('12345678-1234-1234-1234-123456789abc', '1'),
        ):
            with self.subTest(id=id_, source=source):
                value = seal(replace(transaction(), id=id_, source=source))
                self.assertEqual(parse(dumps(value)), value)
        for id_ in ('0', '01', '-1', '+1', '１２３', 'ABCDEFAB-1234-1234-1234-123456789ABC'):
            with self.subTest(id=id_):
                value = seal(replace(transaction(), id=id_))
                with self.assertRaisesRegex(DocumentError, 'invalid transaction ID'):
                    parse(dumps(value))
                value = seal(replace(transaction(), source=id_))
                with self.assertRaisesRegex(DocumentError, 'invalid transaction ID'):
                    parse(dumps(value))

    def test_transaction_metadata_and_images(self):
        text = dumps(transaction())
        without_model = text.replace(f"    model '{DIGEST}'\n", '')
        self.assertNotEqual(without_model, text)
        with self.assertRaisesRegex(DocumentError, 'transaction plan requires a model digest'):
            parse(without_model)
        for modified, message in [
            (text.replace('12345678-1234-1234-1234-123456789abc', 'bad'), 'metadata'),
            (text.replace('123456789abc', '123456789ABC'), 'metadata'),
            (text.replace('12:34:56Z', '12:34:56+00:00'), 'timestamp'),
            (text.replace('2026-10-26', '2026-99-26'), 'metadata'),
            (
                text.replace(
                    '    group first {\n        change', '    group absent {\n        change'
                ),
                'unknown',
            ),
            (text.replace("    time", "    bogus"), 'invalid data document syntax'),
        ]:
            with self.subTest(modified=modified), self.assertRaisesRegex(DocumentError, message):
                parse(modified)
        invalid = (
            "transaction '12345678-1234-1234-1234-123456789abc' { version 1 time '2026-10-26T00:00:00Z' "
            f"digest '{DIGEST}' plan p {{ version 1 model '{DIGEST}' group g {{}} }} "
            "group g { change core.A scope(id=1) {} } }"
        )
        with self.assertRaisesRegex(DocumentError, 'requires a before or after'):
            parse(invalid)
        changed = replace(
            transaction().groups[0].changes[0], before=(), after=(Image('core.A', ('id',), ()),)
        )
        value = seal(replace(transaction(), groups=(TransactionGroup('first', (changed,)),)))
        self.assertEqual(parse(dumps(value)), value)

    def test_transaction_format_does_not_invalidate_digest(self):
        text = dumps(transaction()).replace('    ', ' ')
        formatted = format_document(text)
        self.assertEqual(parse(formatted), transaction())
        self.assertEqual(format_document(formatted), formatted)
        with self.assertRaises(DocumentError):
            format_document(text.replace('version 1', 'version 2'))
