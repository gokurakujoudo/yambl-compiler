"""Exercise transparent definitions, dependent paths and singleton contracts."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.semantics.entities import check_entities
from yambl_compiler.languages.yambl.semantics.expressions import Resolver, Term
from yambl_compiler.languages.yambl.semantics.symbols import World, resolve_scopes
from yambl_compiler.languages.yambl.semantics.types import (
    ENTITY,
    UNKNOWN,
    Resolved,
    accepts,
    common_type,
)
from yambl_compiler.languages.yambl.semantics.validation import check
from yambl_compiler.languages.yambl.syntax.parser import children

VALID = "\nnamespace core\ndecodes Currencies { - 100 USD: 'USD' - 200 CNY: 'CNY' }\ntype money = decimal[18, 4]\ntype selected<t: type<Entity>> = t\ntype codes<d: Decodes> = code<d>\ntype label = string\ntype preference = Hierarchy\nentity Security inherits Entity { dimension dimension_366 { value source: label hierarchy preference: preference } \n    id security_id: snowflake_id\n    static attr currency: code<Currencies>\n    config attr size: money = 12.5\n    @dimension(dimension_366)\n    attr price: money\n    attr related: collection<Security>\n}\nentity Bond inherits Security { @override static attr currency: exact<Currencies.USD> }\nentity Selection {\n    attr kind: type<Security>\n    attr c1: self.kind::value\n    attr c2: self.kind::type\n    attr c3: type<self.kind::value>\n    attr c4: alias<self.kind::value>\n    attr c5: selected<self.kind::value>\n    attr dictionary: Decodes\n    attr code: code<self.dictionary::value>\n    attr codes: codes<Currencies>\n    attr security: Security\n    attr currency: self.security::value.currency::type\n    attr same_currency: alias<self.security::value.currency::value>\n    attr fields: attrs<AttrCats.Static, Security>\n    attr bond_fields: attrs<AttrCats.Static, Bond>\n    attr mapping: mapping<attrs<AttrCats.Static, Security> as Attr, Attr::type>\n    attr shortcut: field_mapping<AttrCats.Static, Security>\n    attr references: mapping<type<Entity>, Security>\n    attr by_code: mapping<code<Currencies>, money>\n    attr by_field: mapping<attrs<AttrCats.Static, Security>, string>\n    attr id_map: mapping<snowflake_id, string>\n    attr aliases: alias<self.mapping::value>\n}\n"

INVALID = [
    (
        'entity A { attr a: self<A> }',
        'a value expression does not accept type arguments',
    ),
    ('entity A { attr a: string as X }', 'as binding is only allowed on a mapping key'),
    ('entity A { attr a: Decodes[1] }', 'invalid parameters for Decodes'),
    ('type loop = loop', 'type dependency cycle'),
    ('type first = second type second = first', 'type dependency cycle'),
    (
        'type bad<t: type<Entity>> = t<string>',
        'a bound parameter does not accept arguments',
    ),
    (
        'type bad<T: type<Entity>, T: type<Entity>> = T',
        'duplicate or invalid type parameter: T',
    ),
    ('type bad = self.kind::value', 'self requires an instance context'),
    ('entity A { attr a: self.b::type attr b: self.a::type }', 'type dependency cycle'),
    ('entity A { attr a: string::type }', ':: requires a field description'),
    ('entity A { attr a: string.nope }', 'field access requires an entity'),
    (
        'entity A { attr a: string attr b: self.a::nope }',
        ':: requires a field description',
    ),
    (
        'entity A { attr a: string attr b: self.a.type }',
        'field access requires an entity',
    ),
    (
        'entity A { attr a: string attr b: A.a::value }',
        'field value requires an instance origin',
    ),
    (
        'entity A { attr a: mapping<attrs<AttrCats.Static, A> as Attr, Attr::value> }',
        'field value requires an instance origin',
    ),
    (
        'entity A { attr a: type<Entity> @override attr b: exact<self.a::value> }',
        'exact requires a constant value',
    ),
    ('entity A { attr a: alias<string> }', 'alias requires an instance field value'),
    ('entity A { attr a: alias<1> }', 'alias requires an instance field value'),
    ('entity A { attr a: attrs<string, A> }', 'attrs requires an AttrCats code'),
    (
        'entity A { attr a: attrs<AttrCats.Static, string> }',
        'attrs requires an entity type value',
    ),
    ('entity A { attr a: attrs<AttrCats.absent, A> }', 'unknown Decodes code'),
    ('entity A {} entity B { attr a: A.absent::type }', 'unknown field: absent'),
    ('entity A { attr a: Entity[1] }', 'invalid parameters for Entity'),
    (
        'type money = decimal entity A { attr a: money[1] }',
        'invalid type arguments for money',
    ),
]


class ExpressionTests(unittest.TestCase):
    def test_finite_values_and_inherited_narrowing(self):
        source = """const LOW: integer = 1 + 1
entity Base { attr choice: one_of[LOW, 3, 4] }
entity Child inherits Base { @override attr choice: one_of[3, LOW] }
entity Leaf inherits Child { @override attr choice: exact<3> }
"""
        self.assertEqual(check([document(source)]), [])
        resolver, fields, _ = self.resolver(source, 'Child')
        resolved = resolver.resolve(fields['choice'][1])
        self.assertEqual({value.value for value in resolved.allowed_values}, {2, 3})
        for replacement in ('one_of[3, 5]', 'exact<4>', 'integer'):
            invalid = source.replace('one_of[3, LOW]', replacement)
            with self.subTest(type=replacement):
                self.assertTrue(
                    any(
                        'override signature mismatch' in d.message
                        for d in check([document(invalid)])
                    )
                )

    def test_finite_value_diagnostics(self):
        for expression in (
            "one_of[]",
            "one_of[1]",
            "one_of[1, 1]",
            "one_of[1, 'x']",
            "one_of[self.x::value(), 1]",
            'one_of[NO_ENTRY, 1]',
        ):
            with self.subTest(expression=expression):
                self.assertTrue(check([document(f'entity A {{ attr x: {expression} }}')]))

    def test_finite_value_cycles_and_evaluation_failures_are_diagnostic(self):
        for source, expected in (
            ('const X: exact<X> = 1', 'constant dependency cycle'),
            ('entity A { attr n: one_of[1 // 0, 2] }', 'finite value evaluation failed'),
            (
                'entity A { attr n: one_of[[1], [2]] }',
                'finite values must be non-collection values',
            ),
        ):
            with self.subTest(source=source):
                self.assertTrue(any(expected in item.message for item in check([document(source)])))

    def test_finite_resolution_without_index_reports_an_error(self):
        for expression, expected in (
            ('one_of[1,2]', 'finite type requires an indexed model'),
            ('exact<1>', 'exact type requires an indexed model'),
        ):
            with self.subTest(expression=expression):
                doc = document(f'entity A {{ attr x: {expression} }}')
                unit = resolve_scopes([doc], [])[0]
                field = children(children(doc.tree, 'entity')[0], 'field')[0]
                body = children(field, 'field_body')[0]
                syntax = children(body, 'type_expr')[0]
                diagnostics = []
                self.assertEqual(Resolver(unit, diagnostics, {}).resolve(syntax), UNKNOWN)
                self.assertTrue(any(expected in issue.message for issue in diagnostics))

    def test_finite_config_default_must_be_a_member(self):
        source = 'entity A { config attr choice: one_of[1, 2] = 2 }'
        self.assertEqual(check([document(source)]), [])
        invalid = source.replace('= 2', '= 3')
        self.assertTrue(
            any('incompatible config default' in d.message for d in check([document(invalid)]))
        )

    def test_finite_constants_normalize_typed_values(self):
        source = """value point { attr x: integer attr y: string }
const FIRST: point = {x: 1, y: 'a'}
const SECOND: point = {x: 2, y: 'b'}
const START: datetime = parse_datetime('2026-01-01T08:00:00+08:00')
decodes D { - 1 One: 'one' - 2 Two: 'two' }
entity A {
    attr point: one_of[FIRST, SECOND]
    attr day: one_of[START, parse_datetime('2026-01-02T00:00:00Z')]
    attr code: one_of[D.One, D.Two]
}
"""
        self.assertEqual(check([document(source)]), [])
        resolver, fields, _ = self.resolver(source, 'A')
        for name in ('point', 'day', 'code'):
            with self.subTest(name=name):
                self.assertEqual(len(resolver.resolve(fields[name][1]).allowed_values), 2)
        duplicate = "decodes D { - 1 Same: 'one' } decodes E { - 1 Same: 'two' } entity A { attr x: one_of[D.Same, E.Same] }"
        self.assertTrue(any('two distinct' in d.message for d in check([document(duplicate)])))

    def test_singleton_types_are_available_without_override(self):
        source = """type active = exact<true>
entity A {
    id key: string
    static attr enabled: active
    static attr label: exact<'ready'>
    config attr count: exact<2> = 2
    static attr choices: values<exact<'ready'>>
    static attr labels: mapping<string, exact<'ready'>>
}
"""
        self.assertEqual(check([document(source)]), [])
        resolver, fields, _ = self.resolver(source, 'A')
        self.assertEqual(resolver.resolve(fields['enabled'][1]).exact, 'true')
        self.assertEqual(resolver.resolve(fields['label'][1]).exact, 'ready')
        self.assertEqual(resolver.resolve(fields['choices'][1]).args[0].exact, 'ready')
        self.assertEqual(resolver.resolve(fields['labels'][1]).args[1].exact, 'ready')
        diagnostics = check([document(source.replace('= 2', '= 3'))])
        self.assertTrue(any('incompatible config default' in item.message for item in diagnostics))

    def resolver(self, source, entity='Selection'):
        units = resolve_scopes([document(source)], [])
        index = check_entities(units, [])
        unit = units[0]
        fields = index.field_types(unit.scope[entity].key)
        return Resolver(unit, [], fields, True), fields, index

    def test_dependent_paths_and_transparent_aliases(self):
        self.assertEqual(check([document(VALID)]), [])
        resolver, fields, _ = self.resolver(VALID)
        resolved = {name: resolver.resolve(syntax) for name, (_, syntax) in fields.items()}
        self.assertEqual(resolved['c1'], resolved['c5'])
        self.assertEqual(resolved['codes'], resolved['currency'])
        self.assertEqual(resolved['c1'].dependency, 'self.kind::value')
        self.assertEqual(resolved['c2'], resolved['kind'])
        self.assertEqual(resolved['c3'].args[0], resolved['c1'])
        self.assertEqual(resolved['c4'].dependency, 'self.kind::value')
        self.assertEqual(resolved['currency'].name, 'code')
        self.assertEqual(resolved['mapping'], resolved['shortcut'])
        self.assertEqual(
            [t.name for t in resolved['fields'].members], ['code', 'decimal', 'type', 'type']
        )
        self.assertEqual(resolved['bond_fields'].members[0].exact, 'USD')
        self.assertEqual(resolved['aliases'].kind, 'collection')
        self.assertEqual(resolver.diagnostics, [])

    def test_string_format_references_use_definition_scope(self):
        sources = [
            "namespace formats type id = string['A|B'] type nested = string[regex: '${id}X']",
            "namespace business from formats import nested type id = string['wrong'] "
            "type combined = string['${nested}_${later}'] type later = string['[0-9]{8}'] "
            "entity Selection { attr result: combined attr escaped: string['\\${id}'] }",
        ]
        units = resolve_scopes([document(source, str(i)) for i, source in enumerate(sources)], [])
        diagnostics = []
        index = check_entities(units, diagnostics)
        fields = index.field_types(units[1].scope['Selection'].key)
        resolver = Resolver(units[1], diagnostics, fields)
        self.assertEqual(
            resolver.resolve(fields['result'][1]).params,
            (('regex', '(?:(?:A|B)X)_(?:[0-9]{8})'),),
        )
        self.assertEqual(resolver.resolve(fields['escaped'][1]).params, (('regex', r'\${id}'),))
        self.assertEqual(diagnostics, [])
        self.assertEqual(check([document(source, str(i)) for i, source in enumerate(sources)]), [])

    def test_string_format_reference_errors(self):
        for source, expected in [
            ("type bad = string['${missing}']", 'unknown string format type: missing'),
            ("type id = integer type bad = string['${id}']", 'requires a constrained string'),
            ("type id = string type bad = string['${id}']", 'requires a constrained string'),
            ("type a = string['${b}'] type b = string['${a}']", 'type dependency cycle'),
        ]:
            with self.subTest(source=source):
                diagnostics = check([document(source)])
                self.assertTrue(any(expected in item.message for item in diagnostics), diagnostics)
                self.assertTrue(all(item.line > 0 and item.column > 0 for item in diagnostics))

    def test_expression_errors_are_located(self):
        for source, expected in INVALID:
            with self.subTest(source=source):
                diagnostics = check([document(source)])
                self.assertTrue(any(expected in d.message for d in diagnostics), diagnostics)
                self.assertTrue(all(d.line > 0 and d.column > 0 for d in diagnostics))

    def test_lexical_alias_scope_and_argument_constraints(self):
        sources = [
            'namespace types decodes Codes {} type item<t: type<Entity>> = t type codes = code<Codes>',
            'namespace business from types import item from types import codes entity A {} entity B { attr a: item<A> attr c: codes }',
        ]
        self.assertEqual(check([document(text, str(i)) for i, text in enumerate(sources)]), [])
        self.assertEqual(
            check(
                [document('type chosen<t: type<Entity>> = type<t> entity A { attr a: chosen<A> }')]
            ),
            [],
        )
        self.assertEqual(
            check([document('type caption<s: string> = string entity A { attr a: caption<A> }')]),
            [],
        )
        self.assertTrue(
            check([document("type chosen<t: type<Entity>> = t entity A { attr a: chosen<'A'> }")])
        )

    def test_singletons_cannot_widen_or_change(self):
        prefix = "decodes Currencies { - 1 USD: 'USD' - 2 CNY: 'CNY' } entity A { attr x: code<Currencies> } entity B inherits A { @override attr x: exact<Currencies.USD> }"
        for spelling, valid in [
            ('exact<Currencies.USD>', True),
            ('exact<Currencies.CNY>', False),
            ('code<Currencies>', False),
        ]:
            source = prefix + f" entity C inherits B {{ @override attr x: {spelling} }}"
            self.assertEqual(not check([document(source)]), valid)
        for primitive, constant in [
            ('string', "'x'"),
            ('integer', '1'),
            ('decimal', '1.2'),
            ('bool', 'true'),
            ('type<Entity>', 'A'),
            ('Decodes', 'Currencies'),
        ]:
            source = f"decodes Currencies {{}} entity A {{ attr x: {primitive} }} entity B inherits A {{ @override attr x: exact<{constant}> }}"
            self.assertEqual(check([document(source)]), [], source)

    def test_singleton_defaults_and_alias_literals(self):
        for literal, valid in [("'USD'", True), ("'CNY'", False)]:
            source = f"entity A {{ config attr x: string }} entity B inherits A {{ @override config attr x: exact<'USD'> = {literal} }}"
            self.assertEqual(not check([document(source)]), valid)
        source = "decodes Codes { - 1 X: 'X' } type singleton = exact<Codes.X> entity A { attr x: code<Codes> } entity B inherits A { @override attr x: singleton }"
        self.assertEqual(check([document(source)]), [])

    def test_overload_common_types_and_tree_boundaries(self):
        source = '''decodes First { - 1 X: 'X' } decodes Second { - 1 Y: 'Y' }
        entity Root {} entity Left inherits Root { @overload attr x: exact<First.X> }
        entity Right inherits Root { @overload attr x: code<Second> }
        entity Other { attr x: integer }
        entity Selection { attr a: attrs<AttrCats.Static, Root> attr b: attrs<AttrCats.Static, Entity> }'''
        self.assertEqual(check([document(source)]), [])
        resolver, fields, index = self.resolver(source)
        self.assertEqual(index.contracts[(('', 'Root'), 'x')].name, 'string')
        self.assertEqual(
            [member.name for member in resolver.resolve(fields['a'][1]).members],
            ['type', 'type', 'string'],
        )
        self.assertEqual(resolver.resolve(fields['b'][1]).members, ())
        self.assertEqual(index.identities[(('', 'Left'), 'x')], (('', 'Root'), 'x'))
        self.assertEqual(index.identities[(('', 'Other'), 'x')], (('', 'Other'), 'x'))

    def test_compatibility_has_no_reverse_string_conversion(self):
        world = World(parents={('', 'Bond'): [('', 'Security')]})
        security = Resolved('Security', 'reference', key=('', 'Security'))
        bond = Resolved('Bond', 'reference', key=('', 'Bond'))
        string = Resolved('string', 'string')
        code = Resolved('code', args=(Resolved('Codes'),))
        self.assertTrue(accepts(security, bond, world))
        self.assertFalse(accepts(bond, security, world))
        self.assertTrue(accepts(string, code, world))
        self.assertTrue(accepts(string, bond, world))
        self.assertFalse(accepts(code, string, world))
        self.assertTrue(accepts(UNKNOWN, string, world))
        self.assertEqual(common_type([bond, security], world), security)
        self.assertEqual(common_type([bond, Resolved('Other', 'reference')], world), ENTITY)
        self.assertEqual(
            common_type(
                [Resolved('type', args=(bond,)), Resolved('type', args=(security,))],
                world,
            ),
            Resolved('type', args=(security,)),
        )
        self.assertIsNone(common_type([], world))
        self.assertIsNone(common_type([Resolved('integer'), Resolved('decimal')], world))
        self.assertIsNone(
            common_type(
                [
                    Resolved('type', args=(Resolved('integer'),)),
                    Resolved('type', args=(Resolved('decimal'),)),
                ],
                world,
            )
        )

    def test_bound_type_values_and_duplicate_diagnostics(self):
        resolver, fields, _ = self.resolver('entity Selection { attr a: type<Entity> }')
        syntax = children(document('type name = t').tree, 'type_alias')[0]
        node = children(syntax, 'type_expr')[0]
        resolver.bindings['t'] = Term(Resolved('type', args=(ENTITY,), exact='Entity'), 'value')
        self.assertEqual(resolver.resolve(node), ENTITY)
        resolver.issue(fields['a'][1], 'one')
        resolver.issue(fields['a'][1], 'one')
        self.assertEqual(len(resolver.diagnostics), 1)
