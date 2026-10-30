"""Syntax diagnostics and lexical boundaries."""

import unittest
from pathlib import Path

from lark import Tree

from tests.support import document
from yambl_compiler.languages.yambl.syntax.parser import (
    Diagnostic,
    children,
    parse,
    string_value,
    tokens,
)


class SyntaxTests(unittest.TestCase):
    def test_exact_closer_and_greater_comparisons(self):
        for source in (
            'entity A { config attr x: exact<1>=1 }',
            'entity A { attr x: exact<1 >= 0> }',
            'entity A { attr x: exact<(1 > 0)> }',
            'entity A { attr x: exact<1> # explanation\n attr y: string }',
        ):
            with self.subTest(source=source):
                self.assertNotIsInstance(parse(source), Diagnostic)

    def test_one_of_contains_calculation_expressions(self):
        doc = document("type options = one_of['a', C.VALUE, to_integer('2'), 1 + 2]")
        alias = children(doc.tree, 'type_alias')[0]
        finite = children(children(alias, 'type_expr')[0], 'one_of_type')[0]
        self.assertEqual(
            [child.data for child in finite.children if isinstance(child, Tree)],
            [
                'calculation_literal',
                'calculation_postfix',
                'calculation_function',
                'sum_expression',
            ],
        )
        self.assertNotIsInstance(parse('type options = one_of[]'), Diagnostic)
        for source in (
            'type x = one_of[1,]',
            'type x = one_of<1, 2>',
            'type x = decimal[1 + 2]',
            'entity A { attr a: exact<1<string>> }',
            'entity A { attr a: exact<1 >= }',
        ):
            with self.subTest(source=source):
                self.assertIsInstance(parse(source), Diagnostic)

    def test_abstract_entity_and_computed_declarations(self):
        doc = document('abstract entity Base { abstract computed attr label: string }')
        entity = children(doc.tree, 'entity')[0]
        self.assertEqual(tokens(children(entity, 'entity_header')[0])[0].type, 'ABSTRACT')
        body = children(children(entity, 'field')[0], 'field_body')[0]
        self.assertEqual([t.type for t in tokens(body)[:3]], ['ABSTRACT', 'COMPUTED', 'ATTR'])
        self.assertEqual(children(body, 'computation'), [])
        for source in (
            'abstract final entity A {}',
            'final abstract entity A {}',
            'entity A { abstract attr x: string }',
            'entity A { abstract computed attr x: string { return \'x\' } }',
            'entity A { computed attr x: string }',
        ):
            with self.subTest(source=source):
                self.assertIsInstance(parse(source), Diagnostic)

    def test_lookup_structure_and_context(self):
        source = '''entity Security {
    lookup by_name {
        name = self.name::value()
        issuer_name = self.issuer::value(
            as_of_date=input.relation_date
        ).name::value()
    }
    lookup by_id { client_id = self.client_id::value() }
}
extend Security { lookup by_market { market = self.market::value() } }
'''
        doc = document(source)
        entity = children(doc.tree, 'entity')[0]
        extension = children(doc.tree, 'extension')[0]
        lookups = children(entity, 'lookup') + children(extension, 'lookup')
        self.assertEqual(
            [str(tokens(node)[1]) for node in lookups], ['by_name', 'by_id', 'by_market']
        )
        keys = children(lookups[0], 'lookup_key')
        self.assertEqual([str(tokens(node)[0]) for node in keys], ['name', 'issuer_name'])
        self.assertEqual((tokens(keys[1])[0].line, tokens(keys[1])[0].column), (4, 9))
        self.assertEqual([str(t) for t in tokens(keys[1])][-4:], ['::', 'value', '(', ')'])
        for invalid in [
            'lookup by_name { x = self.x::value() }',
            'value thing { lookup by_name { x = self.x::value() } }',
            'entity E { lookup { x = self.x::value() } }',
            'entity E { lookup by_name { x self.x::value() } }',
            'entity E { lookup by_name { x = self.x::value() }',
        ]:
            with self.subTest(source=invalid):
                self.assertIsInstance(parse(invalid), Diagnostic)

    def test_markdown_spans_and_unclosed_block(self):
        text = '---\r\n# Markdown { @fake\r\n\r\n---\r\nvalue a { attr x: string }'
        doc = document(text)
        self.assertEqual(tokens(doc.tree)[0].line, 5)
        self.assertEqual(len(doc.blocks), 1)
        result = parse('entity A {}\n---\nunfinished')
        self.assertIsInstance(result, Diagnostic)
        self.assertEqual(
            (result.line, result.column, result.message), (2, 1, 'unclosed Markdown block')
        )
        for text in [
            'final value a {}',
            'value a inherits b {}',
        ]:
            self.assertIsInstance(parse(text), Diagnostic)

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
