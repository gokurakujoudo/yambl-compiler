"""Syntax diagnostics and lexical boundaries."""

import unittest
from pathlib import Path

from yambl_compiler.syntax.parser import Diagnostic, parse


class SyntaxTests(unittest.TestCase):
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
