"""Exercise transparent definitions, dependent paths and singleton contracts."""

import unittest

from tests.support import document
from yambl_compiler.semantics.entities import check_entities
from yambl_compiler.semantics.expressions import Resolver, Term
from yambl_compiler.semantics.symbols import World, resolve_scopes
from yambl_compiler.semantics.types import ENTITY, UNKNOWN, Resolved, accepts, common_type
from yambl_compiler.semantics.validation import check
from yambl_compiler.syntax.parser import children

VALID = '''
namespace core
decodes Currencies { - 100 USD: 'USD' - 200 CNY: 'CNY' }
type money = decimal[18, 4]
type selected<t: type<Entity>> = t
type codes<d: Decodes> = code<d>
type label = string
type preference = Hierarchy
entity Security inherits Entity {
    id security_id: snowflake_id
    static attr currency: code<Currencies>
    config attr size: money = 12.5
    @dimension(source: label, preference: preference)
    attr price: money
    attr related: collection<Security>
}
entity Bond inherits Security { @override static attr currency: exact<Currencies.USD> }
entity Selection {
    attr kind: type<Security>
    attr c1: self.kind::value
    attr c2: self.kind::type
    attr c3: type<self.kind::value>
    attr c4: alias<self.kind::value>
    attr c5: selected<self.kind::value>
    attr dictionary: Decodes
    attr code: code<self.dictionary::value>
    attr codes: codes<Currencies>
    attr security: Security
    attr currency: self.security::value.currency::type
    attr same_currency: alias<self.security::value.currency::value>
    attr fields: attrs<AttrCats.static, Security>
    attr bond_fields: attrs<AttrCats.static, Bond>
    attr mapping: mapping<attrs<AttrCats.static, Security> as Attr, Attr::type>
    attr shortcut: field_mapping<AttrCats.static, Security>
    attr references: mapping<type<Entity>, Security>
    attr by_code: mapping<code<Currencies>, money>
    attr by_field: mapping<attrs<AttrCats.attr, Security>, string>
    attr id_map: mapping<snowflake_id, string>
    attr aliases: alias<self.mapping::value>
}
'''

INVALID = [
    ('entity A { attr a: exact<1<string>> }', 'a value expression does not accept type arguments'),
    ('entity A { attr a: self<A> }', 'a value expression does not accept type arguments'),
    ('entity A { attr a: string as X }', 'as binding is only allowed on a mapping key'),
    ('entity A { attr a: Decodes[1] }', 'invalid parameters for Decodes'),
    ('type loop = loop', 'type dependency cycle'),
    ('type first = second type second = first', 'type dependency cycle'),
    ('type bad<t: type<Entity>> = t<string>', 'a bound parameter does not accept arguments'),
    ('type bad<T: type<Entity>, T: type<Entity>> = T', 'duplicate or invalid type parameter: T'),
    ('type bad = self.kind::value', 'self requires an instance context'),
    ('entity A { attr a: self.b::type attr b: self.a::type }', 'type dependency cycle'),
    ('entity A { attr a: string::type }', ':: requires a field description'),
    ('entity A { attr a: string.nope }', 'field access requires an entity'),
    ('entity A { attr a: string attr b: self.a::nope }', ':: requires a field description'),
    ('entity A { attr a: string attr b: self.a.type }', 'field access requires an entity'),
    ('entity A { attr a: string attr b: A.a::value }', 'field value requires an instance origin'),
    (
        'entity A { attr a: mapping<attrs<AttrCats.attr, A> as Attr, Attr::value> }',
        'field value requires an instance origin',
    ),
    ('entity A { attr a: exact<true> }', 'exact requires override or overload'),
    (
        'entity A { attr a: type<Entity> @override attr b: exact<self.a::value> }',
        'exact requires a constant value',
    ),
    ('entity A { attr a: alias<string> }', 'alias requires an instance field value'),
    ('entity A { attr a: alias<1> }', 'alias requires an instance field value'),
    ('entity A { attr a: attrs<string, A> }', 'attrs requires an AttrCats code'),
    ('entity A { attr a: attrs<AttrCats.attr, string> }', 'attrs requires an entity type value'),
    ('entity A { attr a: attrs<AttrCats.absent, A> }', 'unknown Decodes code'),
    ('entity A {} entity B { attr a: A.absent::type }', 'unknown field: absent'),
    ('entity A { attr a: Entity[1] }', 'invalid parameters for Entity'),
    ('type money = decimal entity A { attr a: money[1] }', 'invalid type arguments for money'),
]


class ExpressionTests(unittest.TestCase):
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
        self.assertEqual([t.name for t in resolved['fields'].members], ['code'])
        self.assertEqual(resolved['bond_fields'].members[0].exact, 'USD')
        self.assertEqual(resolved['aliases'].kind, 'collection')
        self.assertEqual(resolver.diagnostics, [])

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
            check([document('type title<s: string> = string entity A { attr a: title<A> }')]), []
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
            source = prefix + f' entity C inherits B {{ @override attr x: {spelling} }}'
            self.assertEqual(not check([document(source)]), valid)
        for primitive, constant in [
            ('string', "'x'"),
            ('integer', '1'),
            ('decimal', '1.2'),
            ('bool', 'true'),
            ('type<Entity>', 'A'),
            ('Decodes', 'Currencies'),
        ]:
            source = f'decodes Currencies {{}} entity A {{ attr x: {primitive} }} entity B inherits A {{ @override attr x: exact<{constant}> }}'
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
        entity Selection { attr a: attrs<AttrCats.attr, Root> attr b: attrs<AttrCats.attr, Entity> }'''
        self.assertEqual(check([document(source)]), [])
        resolver, fields, index = self.resolver(source)
        self.assertEqual(index.contracts[(('', 'Root'), 'x')].name, 'string')
        self.assertEqual(resolver.resolve(fields['a'][1]).members, (Resolved('string', 'string'),))
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
                [Resolved('type', args=(bond,)), Resolved('type', args=(security,))], world
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
