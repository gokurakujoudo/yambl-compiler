"""Exercise actual plan records, syntax failures and sealed transaction images."""

import unittest
from decimal import Decimal
from unittest.mock import patch

from tests.languages.support import DIGEST, source
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.shared.formatting import format_document
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol
from yambl_compiler.languages.shared.syntax import DocumentError
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.transactions.writing import dumps


class DocumentTests(unittest.TestCase):
    def test_lark_entry_point_and_keyword_boundaries(self):
        from yambl_compiler.languages.yaplan.syntax import parse as parse_plan

        self.assertEqual(parse_plan(source()), Plan('daily', DIGEST, (Group('first', ()),)))
        for text in (
            source().replace('plan daily', 'plandaily'),
            source().replace('version 1', 'version1'),
            source('insertcore.A (id) {}'),
            source('replace core.A.x scopex(id=1) columns(value) {}'),
        ):
            with self.subTest(text=text), self.assertRaises(DocumentError) as caught:
                parse_plan(text)
            self.assertGreaterEqual(caught.exception.line, 1)
            self.assertGreaterEqual(caught.exception.column, 1)
        for body, message in (
            ('delete core.A.x scope(id=1) columns(value) {}', 'cannot contain'),
            ('replace core.A.x scope(id=1)', 'requires columns'),
            ('insert core.A (id) {\n' + '[' * 1000 + '1' + ']' * 1000 + '\n}', 'nesting exceeds'),
        ):
            with self.subTest(body=body), self.assertRaisesRegex(DocumentError, message):
                parse_plan(source(body))

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

    def test_invalid_documents(self):
        cases = [
            ('insert core.A (id) { 1\n}', 'own line'),
            ('insert core.A (id) {\n1 }', 'after data'),
            ('insert core.A (id) {\n1 2\n}', 'own line'),
            ('insert core.A (id) {\n1, 2\n}', 'expected 1 values'),
            ('insert core.A (id) {\n[1,\n2]\n}', 'span multiple'),
            ('insert core.A (id) {\n[1,]\n}', 'invalid data document syntax'),
            ('insert core.A (id) {\n{a:}\n}', 'invalid data document syntax'),
            ('insert core.A (id) {\nfoo(1)\n}', 'invalid data document syntax'),
            ('insert core.A (id) {\n1+2\n}', 'invalid token'),
            ('insert core.A (id, id) {}', 'duplicate column'),
            ('insert core.A () {}', 'invalid data document syntax'),
            ('insert core.A (a.b) {}', 'invalid data document syntax'),
            ('insert 123 (id) {}', 'invalid data document syntax'),
            ('update core.A owned_data delete_all (id) {}', 'only valid'),
            ('insert core.A scope (id=1) columns (id) {}', 'scope requires'),
            ('delete core.A owned_data delete_all scope (id=1)', 'scope requires'),
            ('replace core.A (id) {}', 'requires scope'),
            ('delete core.A.value scope ()', 'owner coordinate'),
            ('delete core.A.value scope (id=1, id=2)', 'duplicate scope'),
            ('delete core.A.value scope (id=1, time in [1,2])', 'invalid data document syntax'),
            ('nonsense core.A (id) {}', 'invalid data document syntax'),
            ('insert core.A (id) {\n\'unfinished\n}', 'unterminated'),
            ('insert core.A (id) {\n' + '[' * 66 + '1' + ']' * 66 + '\n}', 'nesting exceeds'),
        ]
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(DocumentError, message):
                parse(source(body))
        for text, message in [
            (source(metadata=f"version 2 model '{DIGEST}'"), 'unsupported document'),
            (source(metadata="version 1 model 'bad'"), 'SHA-256'),
            (source(metadata='version 1 model 123'), 'invalid data document syntax'),
            (source() + 'extra', 'invalid data document syntax'),
            (source().replace('group first {', 'group first {} group first {'), 'duplicate group'),
            ('', 'invalid data document syntax'),
            ('plan', 'invalid data document syntax'),
            ('plan x {', 'invalid data document syntax'),
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

    def test_model_digest_is_optional_and_round_trips(self):
        text = "plan daily {\n    version 1\n    group first {\n    }\n}\n"
        plan = parse(text)
        self.assertEqual(plan, Plan('daily', None, (Group('first', ()),)))
        self.assertEqual(dumps(plan), text)
        self.assertEqual(format_document(text), text)
        with self.assertRaisesRegex(DocumentError, 'SHA-256'):
            parse(text.replace('    group first', "    model 'bad'\n    group first"))

    def test_empty_objects_inline_empty_comments_and_semantic_guard(self):
        from yambl_compiler.languages.shared.formatting import join_tokens

        text = source('insert core.A (id, value) {\n1, {} #\n}')
        formatted = format_document(text)
        self.assertIn('1, {}  #\n', formatted)
        self.assertEqual(format_document(formatted), formatted)

        # A formatter defect must be caught before any edited text is returned.
        def corrupt(tokens):
            line = join_tokens(tokens)
            return line.replace('1, {}', '2, {}')

        with patch('yambl_compiler.languages.shared.formatting.join_tokens', side_effect=corrupt):
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
