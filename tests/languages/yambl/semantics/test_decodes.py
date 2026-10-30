"""Exercise flag masks, generated zero values and category unions."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.formatting.formatter import format_document
from yambl_compiler.languages.yambl.semantics.entities import check_entities
from yambl_compiler.languages.yambl.semantics.expressions import Resolver
from yambl_compiler.languages.yambl.semantics.symbols import resolve_scopes
from yambl_compiler.languages.yambl.semantics.validation import analyze, check
from yambl_compiler.languages.yambl.syntax.parser import Diagnostic, children, parse

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
    attr removed: attrs<AttrCats.All-AttrCats.Static-AttrCats.Snapshot-AttrCats.Scd-AttrCats.Scd, Base>
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
        'exact requires a constant value',
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
    'type a = AttrCats.Static +',
    'entity A { config attr x: integer = 0b1 }',
    'type a = exact<0b1>',
]


class DecodesTests(unittest.TestCase):
    def test_abstract_inheritance_cycle_reports_error_without_subtypes(self):
        analysis = analyze(
            [document('abstract entity A inherits B {} abstract entity B inherits A {}')]
        )
        self.assertEqual(
            [item.message for item in analysis.diagnostics],
            ['inheritance cycle', 'inheritance cycle'],
        )
        self.assertEqual(analysis.units[0].world.subtypes[('', 'A::subtypes')], ())
        self.assertEqual(analysis.units[0].world.subtypes[('', 'B::subtypes')], ())

    def test_subtypes_promote_abstract_nodes_before_sorting_and_ranking(self):
        analysis = analyze([document('''
entity Root {}
abstract entity Group inherits Root {}
entity Zeta inherits Group {}
@decodes_rank(3, 'First') entity Alpha inherits Group {}
entity Middle inherits Root {}
abstract entity Nested inherits Middle {}
@decodes_rank(7, 'Leaf') entity Leaf inherits Nested {}
''')])
        self.assertEqual(list(analysis.diagnostics), [])
        entries = analysis.units[0].world.subtypes[('', 'Root::subtypes')]
        self.assertEqual(
            [(e.code, e.decode, e.ordering) for e in entries],
            [
                ('Alpha', 'First', 3),
                ('Middle', 'Middle', 200),
                ('Leaf', 'Leaf', 207),
                ('Zeta', 'Zeta', 400),
            ],
        )
        entries = analysis.units[0].world.subtypes[('', 'Group::subtypes')]
        self.assertEqual([(e.code, e.ordering) for e in entries], [('Alpha', 3), ('Zeta', 200)])
        diagnostics = check(
            [
                document(
                    "entity Root {} @decodes_rank(1, 'x') abstract entity Group inherits Root {}"
                )
            ]
        )
        self.assertIn('abstract entity cannot have decodes_rank', [d.message for d in diagnostics])

    def test_subtype_dictionaries_are_independent_and_ranked_depth_first(self):
        sources = [
            document('namespace core entity Security {}'),
            document(
                """namespace bond from core import Security
                @decodes_rank(-2, 'Alpha label') entity Alpha inherits Security {}
                @decodes_rank(decode='Leaf label', k=10) entity Leaf inherits Alpha {}
                @decodes_rank(-2, 'Beta label') entity Beta inherits Security {}
                entity Child inherits Beta {}
                entity Use { attr codes: code<Security::subtypes> }
                type one = exact<Security::subtypes.Alpha>
                const FIRST: code<Security::subtypes> = Security::subtypes.Alpha
                """,
                'bond.yambl',
            ),
        ]
        analysis = analyze(sources)
        self.assertEqual(list(analysis.diagnostics), [])
        world = analysis.units[0].world
        self.assertEqual(
            [
                (e.code, e.decode, e.ordering)
                for e in world.subtypes[('core', 'Security::subtypes')]
            ],
            [
                ('Alpha', 'Alpha label', -2),
                ('Leaf', 'Leaf label', 8),
                ('Beta', 'Beta label', -2),
                ('Child', 'Child', 400),
            ],
        )
        self.assertEqual(
            [(e.code, e.ordering) for e in world.subtypes[('bond', 'Alpha::subtypes')]],
            [('Leaf', 10)],
        )
        self.assertEqual(world.subtypes[('bond', 'Leaf::subtypes')], ())
        self.assertEqual(world.subtypes[('builtins', 'Decodes::subtypes')], ())
        self.assertIn(('bond', 'FIRST'), world.constant_expressions)
        self.assertEqual(
            check(
                [
                    document(
                        'entity A {} entity B inherits A {} entity C { computed attr code: code<A::subtypes> { return A::subtypes.B } }'
                    )
                ]
            ),
            [],
        )

    def test_subtype_dictionary_errors_are_located(self):
        cases = [
            ("@decodes_rank(1, 'x') entity A {}", 'decodes_rank requires an explicit parent'),
            ('entity A {} entity B { attr x: code<Entity::subtypes> }', ':: requires'),
            (
                'entity A {} entity B { attr x: code<A::subtypes> } type bad = exact<A::subtypes.Missing>',
                'unknown Decodes code',
            ),
            (
                "entity A {} entity B { computed attr x: code<A::subtypes> { return 'Missing' } }",
                'unknown Decodes code',
            ),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertTrue(any(expected in d.message for d in check([document(source)])))
        sources = [
            document('namespace base entity Root {}'),
            document(
                'namespace first from base import Root entity Same inherits Root {}', 'first.yambl'
            ),
            document(
                'namespace second from base import Root entity Same inherits Root {}',
                'second.yambl',
            ),
        ]
        self.assertIn('duplicate subtype code: Same', [d.message for d in check(sources)])

    def test_invalid_rank_keeps_default_entries(self):
        for call, expected in (
            ("@decodes_rank('bad', 'x')", 'incompatible decorator argument'),
            ('@decodes_rank(4, 3)', 'incompatible decorator argument'),
            ('@decodes_rank(k=2)', 'missing decorator argument: decode'),
        ):
            with self.subTest(call=call):
                analysis = analyze(
                    [document(f'entity Root {{}} {call} entity Child inherits Root {{}}')]
                )
                self.assertIn(expected, [d.message for d in analysis.diagnostics])
                entries = analysis.units[0].world.subtypes[('', 'Root::subtypes')]
                self.assertEqual(
                    [(e.code, e.decode, e.ordering) for e in entries], [('Child', 'Child', 100)]
                )

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
            [t.name for t in resolved['keys'].members],
            ['string', 'integer', 'bool', 'code', 'decimal', 'type', 'type', 'date'],
        )
        self.assertEqual(resolved['empty'].members, ())
        self.assertEqual([t.name for t in resolved['identity'].members], ['bool', 'decimal'])
        self.assertEqual(
            [t.name for t in resolved['union'].args[0].members],
            ['string', 'integer', 'bool', 'code', 'decimal', 'type', 'type'],
        )
        self.assertIsNone(resolved['symbolic'].args[0].exact)
        self.assertIsNone(resolved['symbolic_difference'].args[0].exact)
        self.assertEqual(resolved['cleared'].members, ())
        self.assertEqual(resolved['absent'].members, ())
        self.assertEqual(resolved['all'], resolved['restored'])
        self.assertEqual(
            [t.name for t in resolved['removed'].members],
            ['bool', 'decimal'],
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
        source = "@flags #bits\ndecodes Codes{- 0b0001 Read:'Read'}\ntype fields=attrs<AttrCats.Static+AttrCats.None,Entity>"
        expected = "@flags  # bits\ndecodes Codes {\n    - 0b0001 Read: 'Read'\n}\ntype fields = attrs<AttrCats.Static + AttrCats.None, Entity>\n"
        formatted = format_document(document(source))
        self.assertEqual(formatted, expected)
        self.assertEqual(format_document(document(formatted)), formatted)
        self.assertEqual(
            len(children(children(document(formatted).tree, 'decodes')[0], 'entry')), 1
        )
