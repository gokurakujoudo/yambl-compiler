"""Exact source formatting and idempotence."""

import unittest

from tests.support import document
from yambl_compiler.formatting.formatter import format_document


class FormattingTests(unittest.TestCase):
    def test_exactly_one_terminal_newline(self):
        for ending in ['', '\n', '\n\n', '\n\n\n', '\r\n\r\n', '\n  \n\t']:
            with self.subTest(ending=repr(ending)):
                formatted = format_document(document('entity A {}' + ending))
                self.assertEqual(formatted, 'entity A {\n}\n')
                self.assertEqual(format_document(document(formatted)), formatted)

    def test_comments_inside_multiline_syntax(self):
        source = "entity #name\nA { attr x: map< #key\nstring, #value\ndecimal[18, 4]> #field\n}"
        formatted = format_document(document(source))
        self.assertEqual(
            formatted,
            "entity A {  # name\n    attr x: map<string, decimal[18, 4]>  # key\n    # value\n    # field\n}\n",
        )
        self.assertEqual(format_document(document(formatted)), formatted)

    def test_format_exact_and_idempotent(self):
        source = "#head\r\nnamespace  core\r\n\r\n\r\nfrom decodes import Currencies\r\n\r\nentity Fund{#entity\r\n\t# field\r\nattr price : decimal[ precision:18,scale:4] #price\r\n@temporal( as_of_date:date ) snapshot attr assets:collection<Fund>\r\nattr pattern:string[regex:'[# \\'x]+']\r\n}\r\n\r\ndecodes Currencies{- 100 CNY:'人民币' - 200 USD:'USD'}#end"
        expected = "# head\nnamespace core\n\nfrom decodes import Currencies\n\nentity Fund {  # entity\n    # field\n    attr price: decimal[precision: 18, scale: 4]  # price\n    @temporal(as_of_date: date)\n    snapshot attr assets: collection<Fund>\n    attr pattern: string[regex: '[# \\'x]+']\n}\n\ndecodes Currencies {\n    - 100 CNY: '人民币'\n    - 200 USD: 'USD'\n}  # end\n"
        formatted = format_document(document(source))
        self.assertEqual(formatted, expected)
        self.assertEqual(format_document(document(formatted)), expected)
        self.assertEqual(format_document(document('')), '\n')
        self.assertEqual(format_document(document('  #solo\n\n\n')), '# solo\n')
        self.assertEqual(
            format_document(document('namespace a.b\nentity Fund{}')),
            'namespace a.b\nentity Fund {\n}\n',
        )
