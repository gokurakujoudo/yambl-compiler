"""Syntax diagnostics and lexical boundaries."""

import unittest
from pathlib import Path

from tests.support import document
from yambl_compiler.syntax.parser import Diagnostic, parse, string_value, tokens


class SyntaxTests(unittest.TestCase):
    def test_escaped_quotes_and_literal_backslashes(self):
        parsed = document(
            r"entity A { @description('Security\'s # currency \\ path \d') attr x: string }"
        )
        value = next(token for token in tokens(parsed.tree) if token.type == 'STRING')
        self.assertEqual(string_value(value), "Security's # currency \\ path \\d")

    def test_syntax_and_locations(self):
        for source in [
            'entity Fund',
            'entity Fund { attr x: HoldingEntry[] }',
            "decodes Currencies { - 1 CNY: 'unterminated }",
            'namespace a namespace b',
            'entity Fund { ref x: Fund }',
            'from a import *',
            'entity Fund: Decodes {}',
            'entityFund {}',
            'namespacecore entity Fund {}',
            'entity Fund { attrx: string }',
        ]:
            with self.subTest(source=source):
                problem = parse(source, Path('bad.yambl'))
                self.assertIsInstance(problem, Diagnostic)
                self.assertIn('bad.yambl:', str(problem))
                self.assertIn('error: invalid YAMBL syntax', str(problem))
        problem = parse('entity Fund {\n    ?\n}')
        self.assertIsInstance(problem, Diagnostic)
        self.assertEqual((problem.line, problem.column), (2, 5))
