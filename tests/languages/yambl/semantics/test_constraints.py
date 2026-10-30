"""Check ordered entity constraint declarations and expression scopes."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.semantics.validation import analyze


class ConstraintTests(unittest.TestCase):
    def test_order_and_extension_scope(self):
        result = analyze([document('''
            entity A { attr n: integer constraint good { let x = self.n::value() assert x > 0 let y = 2 assert y > x } }
            extend A { constraint other {} }
            entity B inherits A { constraint child { assert true } }
        ''')])
        self.assertEqual([d.message for d in result.diagnostics if d.severity == 'error'], [])
        constraint = result.constraints[('', 'A')]['good']
        self.assertEqual(
            [item.kind for item in constraint.statements], ['let', 'assert', 'let', 'assert']
        )
        self.assertEqual(constraint.statements[1].assert_source, 'assert x > 0')

    def test_duplicate_inherited_name_unknown_local_input_and_non_bool(self):
        source = '''entity A {
            snapshot attr n: integer
            constraint invalid { assert later > 0 let later = 1 assert self.n::value() > 0 assert 2 }
        }
        extend A { constraint invalid {} }
        entity B inherits A { constraint invalid {} }
        '''
        messages = [d.message for d in analyze([document(source)]).diagnostics]
        self.assertTrue(any('unknown calculation name: later' in item for item in messages))
        self.assertTrue(
            any('constraint cannot have external inputs: as_of_date' in item for item in messages)
        )
        self.assertTrue(any('constraint assert requires bool' in item for item in messages))
        self.assertTrue(any('duplicate constraint' in item for item in messages))
        self.assertTrue(any('duplicate inherited constraint' in item for item in messages))

    def test_invalid_constraint_and_local_binding_names_and_types(self):
        result = analyze([document("""
entity A {
    constraint BadName {
        let value: string = 1
        let value = 2
        assert true
    }
}
""")])
        messages = [d.message for d in result.diagnostics]
        self.assertTrue(
            any('constraint names must use snake_case' in message for message in messages)
        )
        self.assertTrue(any('incompatible local binding' in message for message in messages))
        self.assertTrue(
            any('duplicate or invalid local binding' in message for message in messages)
        )

    def test_return_is_not_a_constraint_statement(self):
        from yambl_compiler.languages.yambl.syntax.parser import Diagnostic, parse

        self.assertIsInstance(parse('entity A { constraint bad { return true } }'), Diagnostic)
