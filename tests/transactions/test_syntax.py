"""Exercise actual plan records, syntax failures and sealed transaction images."""

import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from yambl_compiler.transactions.formatting import format_document
from yambl_compiler.transactions.model import (
    Change,
    Condition,
    Group,
    Image,
    ObjectValue,
    Operation,
    Plan,
    Symbol,
    Transaction,
    TransactionGroup,
)
from yambl_compiler.transactions.syntax import DocumentError, Parser, parse, tokenize
from yambl_compiler.transactions.writing import dumps, literal, seal, semantic_digest

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


class SyntaxTests(unittest.TestCase):
    def test_digest_numeric_expansion_failure_is_a_document_diagnostic(self):
        text = dumps(transaction())
        for error in (MemoryError, OverflowError, ValueError):
            with (
                self.subTest(error=error),
                patch('yambl_compiler.transactions.writing.semantic_digest', side_effect=error),
            ):
                with self.assertRaisesRegex(DocumentError, 'numeric representation'):
                    parse(text)

    def test_shared_sealed_image_cases(self):
        fixture = (
            Path(__file__).resolve().parents[3]
            / 'yambl/tools/vscode/lsp/tests/transaction-image-cases.json'
        )
        for case in json.loads(fixture.read_text(encoding='utf-8'))['cases']:
            with self.subTest(case=case['name']):
                document = parse(case['text'])
                self.assertIsInstance(document, Transaction)
                self.assertEqual(parse(format_document(case['text'])), document)

    def test_shared_editor_cases(self):
        fixture = (
            Path(__file__).resolve().parents[3]
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

    def test_oversized_integer_and_ambiguous_programmatic_symbol(self):
        with self.assertRaisesRegex(DocumentError, 'numeric literal'):
            parse(source('insert core.A (id) {\n' + '1' * 5000 + '\n}'))
        with self.assertRaisesRegex(ValueError, 'round-trip'):
            dumps(
                Plan(
                    'p',
                    DIGEST,
                    (Group('g', (Operation('insert', 'core.A', ('id',), ((Symbol('true'),),)),)),),
                )
            )

    def test_all_operations_and_literal_types(self):
        text = source(
            """insert core.Security (security_id, currency, coupon, active, tags, metadata) {
    'S1', decodes.Currencies.CNY, -12345678901234567890.1200, true, ['a', 'b'], {name: 'x', 'other': false}
}
update core.Security (security_id, coupon) {
    'S1', 1e-20
}
delete core.Security owned_data delete_all (security_id) {
    'S2'
}
replace core.Security.price scope (security_id='S1', as_of_date in ['2026-01-01', '2026-02-01')) columns (as_of_date, value) {
    '2026-01-05', NO_ENTRY
}
delete core.Security.price scope (security_id='S1')
"""
        )
        plan = parse(text)
        self.assertIsInstance(plan, Plan)
        operations = plan.groups[0].operations
        self.assertEqual(
            [o.kind for o in operations], ['insert', 'update', 'delete', 'replace', 'delete']
        )
        self.assertEqual(
            operations[0].rows[0],
            (
                'S1',
                Symbol('decodes.Currencies.CNY'),
                Decimal('-12345678901234567890.1200'),
                True,
                ('a', 'b'),
                ObjectValue(((Symbol('name'), 'x'), ('other', False))),
            ),
        )
        self.assertEqual(operations[1].rows[0][1], Decimal('0.00000000000000000001'))
        self.assertTrue(operations[2].owned_data)
        self.assertEqual(
            operations[3].scope[-1], Condition('as_of_date', '2026-01-01', '2026-02-01')
        )
        self.assertEqual(operations[4].rows, ())
        self.assertEqual(parse(dumps(plan)), plan)

    def test_yambl_string_escapes_and_crlf(self):
        text = source(r"""insert core.A (id, text) {
    12, 'quote\' slash\\ regex\d hash#'
}""").replace('\n', '\r\n').rstrip('\r\n')
        plan = parse(text)
        self.assertEqual(
            plan.groups[0].operations[0].rows[0], (12, "quote' slash\\ regex\\d hash#")
        )
        self.assertEqual(parse(dumps(plan)), plan)

    def test_error_coordinates(self):
        with self.assertRaises(DocumentError) as caught:
            tokenize('plan x {\n  @')
        self.assertEqual((caught.exception.line, caught.exception.column), (2, 3))
        self.assertIn('2:3:', str(caught.exception))

    def test_invalid_documents(self):
        cases = [
            ('insert core.A (id) { 1\n}', 'own line'),
            ('insert core.A (id) {\n1 }', 'after data'),
            ('insert core.A (id) {\n1 2\n}', 'after data'),
            ('insert core.A (id) {\n1, 2\n}', 'expected 1 values'),
            ('insert core.A (id) {\n[1,\n2]\n}', 'span multiple'),
            ('insert core.A (id) {\n[1,]\n}', 'expected literal'),
            ('insert core.A (id) {\n{a:}\n}', 'expected literal'),
            ('insert core.A (id) {\nfoo(1)\n}', 'after data'),
            ('insert core.A (id) {\n1+2\n}', 'invalid token'),
            ('insert core.A (id, id) {}', 'duplicate column'),
            ('insert core.A () {}', 'expected identifier'),
            ('insert core.A (a.b) {}', 'expected identifier'),
            ('insert 123 (id) {}', 'expected identifier'),
            ('update core.A owned_data delete_all (id) {}', 'only valid'),
            ('insert core.A scope (id=1) columns (id) {}', 'scope requires'),
            ('delete core.A owned_data delete_all scope (id=1)', 'scope requires'),
            ('replace core.A (id) {}', 'requires scope'),
            ('delete core.A.value scope ()', 'owner coordinate'),
            ('delete core.A.value scope (id=1, id=2)', 'duplicate scope'),
            ('delete core.A.value scope (id=1, time in [1,2])', r'expected \)'),
            ('nonsense core.A (id) {}', 'expected insert'),
            ('insert core.A (id) {\n\'unfinished\n}', 'unterminated'),
            ('insert core.A (id) {\n' + '[' * 66 + '1' + ']' * 66 + '\n}', 'nesting exceeds'),
        ]
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(DocumentError, message):
                parse(source(body))
        for text, message in [
            (source(metadata=f"version 2 model '{DIGEST}'"), 'unsupported document'),
            (source(metadata="version 1 model 'bad'"), 'SHA-256'),
            (source(metadata='version 1 model 123'), 'single-quoted'),
            (source() + 'extra', 'unexpected content'),
            (source().replace('group first {', 'group first {} group first {'), 'duplicate group'),
            ('', 'expected transaction'),
            ('plan', 'identifier'),
            ('plan x {', 'expected version'),
            ('\ufeff' + source(), 'invalid token'),
        ]:
            with self.subTest(text=text), self.assertRaisesRegex(DocumentError, message):
                parse(text)

    def test_empty_data_and_empty_plan(self):
        plan = parse(source('insert core.A (id) {}'))
        self.assertEqual(plan.groups[0].operations[0].rows, ())
        self.assertEqual(
            parse(f"plan empty {{ version 1 model '{DIGEST}' }}"), Plan('empty', DIGEST, ())
        )

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

    def test_transaction_metadata_and_images(self):
        text = dumps(transaction())
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
            (text.replace("    time", "    bogus"), 'expected time'),
        ]:
            with self.subTest(modified=modified), self.assertRaisesRegex(DocumentError, message):
                parse(modified)
        parser = Parser(
            "transaction '12345678-1234-1234-1234-123456789abc' { version 1 time '2026-10-26T00:00:00Z' "
            f"digest '{DIGEST}' plan p {{ version 1 model '{DIGEST}' group g {{}} }} "
            "group g { change core.A scope(id=1) {} } }"
        )
        with self.assertRaisesRegex(DocumentError, 'requires a before or after'):
            parser.transaction()
        changed = replace(
            transaction().groups[0].changes[0], before=(), after=(Image('core.A', ('id',), ()),)
        )
        value = seal(replace(transaction(), groups=(TransactionGroup('first', (changed,)),)))
        self.assertEqual(parse(dumps(value)), value)

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


class FormattingTests(unittest.TestCase):
    def test_empty_objects_inline_empty_comments_and_semantic_guard(self):
        from yambl_compiler.transactions.formatting import join_tokens

        text = source('insert core.A (id, value) {\n1, {} #\n}')
        formatted = format_document(text)
        self.assertIn('1, {}  #\n', formatted)
        self.assertEqual(format_document(formatted), formatted)

        # A formatter defect must be caught before any edited text is returned.
        def corrupt(tokens):
            line = join_tokens(tokens)
            return line.replace('1, {}', '2, {}')

        with patch('yambl_compiler.transactions.formatting.join_tokens', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'changed document semantics'):
                format_document(text)

    def test_comments_literals_and_indentation(self):
        text = source("""# rows
insert core.A ( id , value ) {
1 , 'hash#' #  note
2,1.2500
3,{name:'x', tags:[]}
}


# end
""")
        formatted = format_document(text)
        self.assertIn("            1, 'hash#'  # note\n", formatted)
        self.assertIn('            2, 1.2500\n', formatted)
        self.assertIn('        # rows\n', formatted)
        self.assertIn('        # end\n', formatted)
        self.assertNotIn('\n\n\n', formatted)
        self.assertTrue(formatted.endswith('}\n'))
        self.assertEqual(parse(formatted), parse(text))
        self.assertEqual(format_document(formatted), formatted)

    def test_inline_headers_empty_comments_and_continuations(self):
        text = f"plan p {{ version 1 model '{DIGEST}' group g {{\n#\nreplace core.A.x\nscope(id=1)\ncolumns(value) {{\n[]\n}}\n}}}} # tail\n"
        formatted = format_document(text)
        self.assertEqual(parse(formatted), parse(text))
        self.assertIn('\n    version 1\n', formatted)
        self.assertIn('            scope (id=1)\n', formatted)
        self.assertIn('# tail', formatted)
        self.assertEqual(format_document(formatted), formatted)
        self.assertEqual(
            format_document('# leading\n' + source() + '# trailing'),
            format_document(format_document('# leading\n' + source() + '# trailing')),
        )

    def test_transaction_format_does_not_invalidate_digest(self):
        text = dumps(transaction()).replace('    ', ' ')
        formatted = format_document(text)
        self.assertEqual(parse(formatted), transaction())
        self.assertEqual(format_document(formatted), formatted)
        with self.assertRaises(DocumentError):
            format_document(text.replace('version 1', 'version 2'))


if __name__ == '__main__':
    unittest.main()
