"""Exact source formatting and idempotence."""

import unittest

from tests.support import document
from yambl_compiler.formatting.formatter import format_document


class FormattingTests(unittest.TestCase):
    def test_markdown_body_is_opaque_and_preserves_whitespace(self):
        source = '---\r\n# Heading  \r\n\r\n\r\n    code { } \' @dimensions\r\n---\r\nvalue a{\r\n    ---\r\n  body  \r\n\r\n    ---\r\nattr x:string #note\r\n}\r\n'
        expected = '---\n# Heading  \n\n\n    code { } \' @dimensions\n---\nvalue a {\n    ---\n  body  \n\n    ---\n    attr x: string  # note\n}\n'
        self.assertEqual(format_document(document(source)), expected)
        self.assertEqual(format_document(document(expected)), expected)

    def test_dependent_types_and_relationship_markers(self):
        source = "final entity Selection{attr selected:type<Entity> @overload config attr limit:integer=10 attr when:field_mapping<AttrCats.Static,self.selected::value>}#end"
        expected = 'final entity Selection {\n    attr selected: type<Entity>\n    @overload\n    config attr limit: integer = 10\n    attr when: field_mapping<AttrCats.Static, self.selected::value>\n}  # end\n'
        self.assertEqual(format_document(document(source)), expected)
        self.assertEqual(format_document(document(expected)), expected)

    def test_extensions_metadata_and_defaults(self):
        source = "@title( 'Security' ) entity Security{}\n\nextend Security{@description('Security\\'s # price') @dimension(source:code<'PriceSources'>) @temporal(as_of_date:date) snapshot attr price:decimal config attr days:integer=20}\n\nentity Bond inherits Security{@temporal(as_of_date:date) scd attr notional:decimal}"
        expected = "@title('Security')\nentity Security {\n}\n\nextend Security {\n    @description('Security\\'s # price')\n    @dimension(source: code<'PriceSources'>)\n    @temporal(as_of_date: date)\n    snapshot attr price: decimal\n    config attr days: integer = 20\n}\n\nentity Bond inherits Security {\n    @temporal(as_of_date: date)\n    scd attr notional: decimal\n}\n"
        formatted = format_document(document(source))
        self.assertEqual(formatted, expected)
        self.assertEqual(format_document(document(formatted)), expected)

    def test_exactly_one_terminal_newline(self):
        for ending in ['', '\n', '\n\n', '\n\n\n', '\r\n\r\n', '\n  \n\t']:
            with self.subTest(ending=repr(ending)):
                formatted = format_document(document('entity A {}' + ending))
                self.assertEqual(formatted, 'entity A {\n}\n')
                self.assertEqual(format_document(document(formatted)), formatted)

    def test_comments_inside_multiline_syntax(self):
        source = (
            "entity #name\nA { attr x: mapping< #key\nstring, #value\ndecimal[18, 4]> #field\n}"
        )
        formatted = format_document(document(source))
        self.assertEqual(
            formatted,
            "entity A {  # name\n    attr x: mapping<string, decimal[18, 4]>  # key\n    # value\n    # field\n}\n",
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
