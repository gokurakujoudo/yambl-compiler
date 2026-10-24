"""Exercise flag masks, generated zero values and category unions."""

import unittest

from tests.support import document
from yambl_compiler.formatting.formatter import format_document
from yambl_compiler.semantics.entities import check_entities
from yambl_compiler.semantics.expressions import Resolver
from yambl_compiler.semantics.symbols import resolve_scopes
from yambl_compiler.semantics.validation import check
from yambl_compiler.syntax.parser import Diagnostic, children, parse

VALID = '''@title('Access')
@flags
@description('Independent permissions')
decodes Permissions { - 0b001 Read: 'Read' - 2 Write: 'Write' }
type selected<a: code<AttrCats>, b: type<Entity>> = field_mapping<a + AttrCats.None, b>
entity Base {
    id key: string
    attr label: string
    static attr rank: integer
    config attr enabled: bool = true
    attr many: values<string>
    attr permissions: code<Permissions>
}
extend Base { config attr size: decimal }
entity Child inherits Base {
    snapshot attr date: date
    @override attr permissions: exact<Permissions.Read + Permissions.Write + Permissions.Read + Permissions.None>
}
entity Grandchild inherits Child {
    @override attr permissions: exact<Permissions.All - Permissions.Write + Permissions.Write>
}
entity Selection {
    attr union: selected<AttrCats.Static + AttrCats.Config, Base>
    attr keys: attrs<AttrCats.Static + AttrCats.Config + AttrCats.Snapshot, Base>
    attr empty: attrs<AttrCats.None, Base>
    attr identity: attrs<AttrCats.None + AttrCats.Config + AttrCats.Config, Base>
    attr all: attrs<AttrCats.All, Base>
    attr removed: attrs<AttrCats.All-AttrCats.Attr-AttrCats.Snapshot-AttrCats.Scd-AttrCats.Scd, Base>
    attr cleared: attrs<AttrCats.All - AttrCats.All, Base>
    attr absent: attrs<AttrCats.None - AttrCats.Config - AttrCats.Config, Base>
    attr restored: attrs<AttrCats.All - AttrCats.Config + AttrCats.Config, Base>
    attr flags: code<Permissions>
    attr symbolic: attrs<self.categories::value + AttrCats.Static, Base>
    attr symbolic_difference: attrs<self.categories::value - AttrCats.Static, Base>
    attr categories: code<AttrCats>
}
'''

INVALID = [
    ("@flags decodes Codes { - 1 All: 'All' }", 'flags code All is generated automatically'),
    (
        "entity A { attr x: attrs<AttrCats.All-1, A> }",
        '- requires codes from the same flags Decodes',
    ),
    (
        "entity A { attr x: attrs<AttrCats.All - -1, A> }",
        '- requires codes from the same flags Decodes',
    ),
    (
        "entity A { attr x: attrs<AttrCats.All - string, A> }",
        '- requires codes from the same flags Decodes',
    ),
    (
        "@flags decodes Codes { - 1 A: 'x' } entity A { attr x: attrs<AttrCats.All - Codes.All, A> }",
        '- requires codes from the same flags Decodes',
    ),
    (
        "decodes Codes { - 1 A: 'x' } type chosen<a: code<Codes>> = string entity A { attr x: chosen<Codes.A - Codes.A> }",
        '- requires codes from the same flags Decodes',
    ),
    (
        "decodes Codes {} type chosen<a: code<Codes>> = string entity A { attr x: chosen<Codes.All> }",
        'unknown Decodes code: Codes.All',
    ),
    ('@flags entity A {}', 'decorator flags does not allow this target'),
    ('@flags value pair {}', 'decorator flags does not allow this target'),
    ("decodes Codes { - 1 lower: 'x' }", 'Decodes codes must use PascalCase'),
    ("decodes Codes { - 1 Snake_Case: 'x' }", 'Decodes codes must use PascalCase'),
    ("@flags @flags decodes Codes {}", 'duplicate flags annotation'),
    ("@flags decodes Codes { - 1 None: 'x' }", 'flags code None is generated automatically'),
    ("@flags decodes Codes { - 0 Zero: 'x' }", 'flags entries require a positive single-bit mask'),
    (
        "@flags decodes Codes { - -1 Negative: 'x' }",
        'flags entries require a positive single-bit mask',
    ),
    (
        "@flags decodes Codes { - -0b10 Negative: 'x' }",
        'flags entries require a positive single-bit mask',
    ),
    (
        "@flags decodes Codes { - 0b11 Both: 'x' }",
        'flags entries require a positive single-bit mask',
    ),
    ("@flags decodes Codes { - 1 A: 'x' - 0b01 B: 'y' }", 'duplicate flags mask'),
    ("@flags decodes Codes { - 1 A: 'x' - 2 A: 'y' }", 'duplicate code in Decodes: A'),
    (
        "entity A { attr x: attrs<AttrCats.Config + 1, A> }",
        '+ requires codes from the same flags Decodes',
    ),
    (
        "entity A { attr x: attrs<string + AttrCats.Config, A> }",
        '+ requires codes from the same flags Decodes',
    ),
    (
        "@flags decodes Codes { - 1 A: 'x' } entity A { attr x: attrs<AttrCats.Config + Codes.A, A> }",
        '+ requires codes from the same flags Decodes',
    ),
    (
        "decodes Codes { - 1 A: 'x' } type chosen<a: code<Codes>> = string entity A { attr x: chosen<Codes.A + Codes.A> }",
        '+ requires codes from the same flags Decodes',
    ),
    (
        "entity A { attr x: code<Missing> attr y: exact<self.x::value + self.x::value> }",
        '+ requires codes from the same flags Decodes',
    ),
    (
        "decodes Codes {} type chosen<a: code<Codes>> = string entity A { attr x: chosen<Codes.None> }",
        'unknown Decodes code: Codes.None',
    ),
    (
        "@flags decodes Codes {} type chosen<a: code<Codes>> = string entity A { attr x: chosen<Codes.Missing + Codes.None> }",
        'unknown Decodes code: Codes.Missing',
    ),
]

SYNTAX_ERRORS = [
    "@flags decodes Codes { - 0b12 A: 'x' }",
    'type a = AttrCats.Attr +',
    'entity A { config attr x: integer = 0b1 }',
    'type a = exact<0b1>',
]


class DecodesTests(unittest.TestCase):
    def test_combined_categories_and_singleton_equivalence(self):
        doc = document(VALID)
        self.assertEqual(check([doc]), [])
        units = resolve_scopes([doc], [])
        index = check_entities(units, [])
        unit = units[0]
        fields = index.field_types(unit.scope['Selection'].key)
        resolver = Resolver(unit, [], fields)
        resolved = {name: resolver.resolve(syntax) for name, (_, syntax) in fields.items()}
        self.assertEqual(
            [t.name for t in resolved['keys'].members], ['integer', 'bool', 'decimal', 'date']
        )
        self.assertEqual(resolved['empty'].members, ())
        self.assertEqual([t.name for t in resolved['identity'].members], ['bool', 'decimal'])
        self.assertEqual(
            [t.name for t in resolved['union'].args[0].members], ['integer', 'bool', 'decimal']
        )
        self.assertIsNone(resolved['symbolic'].args[0].exact)
        self.assertIsNone(resolved['symbolic_difference'].args[0].exact)
        self.assertEqual(resolved['cleared'].members, ())
        self.assertEqual(resolved['absent'].members, ())
        self.assertEqual(resolved['all'], resolved['restored'])
        self.assertEqual(
            [t.name for t in resolved['removed'].members], ['integer', 'bool', 'decimal']
        )
        self.assertEqual(resolver.diagnostics, [])

    def test_flags_diagnostics_and_pascal_case(self):
        for source, expected in INVALID:
            with self.subTest(source=source):
                diagnostics = check([document(source)])
                self.assertTrue(any(expected in d.message for d in diagnostics), diagnostics)
                self.assertTrue(all(d.line > 0 and d.column > 0 for d in diagnostics))
        for source in SYNTAX_ERRORS:
            with self.subTest(source=source):
                self.assertIsInstance(parse(source), Diagnostic)

    def test_zero_and_normal_dictionary_codes(self):
        for source in [
            "decodes Codes { - 0 None: 'None' - 0b10 USD: 'USD' - -3 SomeValue: 'Some value' }",
            "@flags decodes Codes {} type chosen<a: code<Codes>> = string entity A { attr x: chosen<Codes.None + Codes.None> }",
            "@flags decodes Codes {} entity A { attr x: code<Codes> } entity B inherits A { @override attr x: exact<Codes.All> } entity C inherits B { @override attr x: exact<Codes.None> }",
            "@flags decodes Codes { - " + str(1 << 512) + " High: 'High' }",
            "@flags decodes Codes { - 0b1" + '0' * 512 + " High: 'High' }",
        ]:
            self.assertEqual(check([document(source)]), [])
        sources = [
            "namespace codes @flags decodes Rights { - 1 Read: 'Read' - 2 Write: 'Write' } type chosen<a: code<Rights>> = string",
            "namespace app from codes import Rights from codes import chosen entity A { attr x: chosen<Rights.Read + Rights.Write> }",
        ]
        self.assertEqual(check([document(s, str(i)) for i, s in enumerate(sources)]), [])

    def test_format_keeps_flags_and_binary_spelling(self):
        source = "@flags #bits\ndecodes Codes{- 0b0001 Read:'Read'}\ntype fields=attrs<AttrCats.Attr+AttrCats.None,Entity>"
        expected = "@flags  # bits\ndecodes Codes {\n    - 0b0001 Read: 'Read'\n}\ntype fields = attrs<AttrCats.Attr + AttrCats.None, Entity>\n"
        formatted = format_document(document(source))
        self.assertEqual(formatted, expected)
        self.assertEqual(format_document(document(formatted)), formatted)
        self.assertEqual(
            len(children(children(document(formatted).tree, 'decodes')[0], 'entry')), 1
        )
