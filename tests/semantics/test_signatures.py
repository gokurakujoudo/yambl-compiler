"""Field signatures compare resolved contracts without default values."""

import unittest

from tests.support import document
from yambl_compiler.semantics.entities import check_entities
from yambl_compiler.semantics.signatures import field_signature, type_signature
from yambl_compiler.semantics.symbols import resolve_scopes
from yambl_compiler.syntax.parser import children


class SignatureTests(unittest.TestCase):
    def test_dimension_selector_association_is_part_of_signature(self):
        from yambl_compiler.semantics.validation import check

        shared = 'entity Base {} entity A inherits Base { @overload @dimension(x: string, a: Hierarchy) @dimension(y: string, b: Hierarchy) attr z: string } '
        same = 'entity B inherits Base { @overload @dimension(y: string, b: Hierarchy) @dimension(x: string, a: Hierarchy) attr z: string }'
        different = same.replace('y: string, b:', 'y: string, a:').replace(
            'x: string, a:', 'x: string, b:'
        )
        self.assertEqual(check([document(shared + same)]), [])
        self.assertTrue(
            any('identical signatures' in d.message for d in check([document(shared + different)]))
        )

    def test_unbounded_integer_parameters_and_signed_zero(self):
        large = '9' * 5000
        for left, right in [
            ('-0018, -0', 'precision: -18, scale: 0'),
            (large + ', 4', 'scale: 4, precision: 0' + large),
        ]:
            source = document(
                f'entity Base {{}} entity A inherits Base {{ @overload attr x: decimal[{left}] }} entity B inherits Base {{ @overload attr x: decimal[{right}] }}'
            )
            diagnostics = []
            check_entities(resolve_scopes([source], diagnostics), diagnostics)
            self.assertEqual(diagnostics, [])

    def test_equivalent_parameter_spelling_metadata_order_and_default(self):
        source = document("""entity A {
            @title('Price') @description('Daily')
            @dimension(source: string) @dimension(unit: string)
            attr x: decimal[18, 4]
            @dimension(unit: string) @dimension(source: string)
            @description('Daily') @title('Price')
            attr y: decimal[scale: 4, precision: 18]
            attr z: string['a\\\\b']
            attr w: string[regex: 'a\\\\b']
        }""")
        from yambl_compiler.semantics.validation import check

        self.assertEqual(check([source]), [])
        units = resolve_scopes([source], [])
        fields = children(children(source.tree, 'entity')[0], 'field')
        self.assertEqual(
            field_signature(fields[0], units[0], {}), field_signature(fields[1], units[0], {})
        )
        self.assertEqual(
            field_signature(fields[2], units[0], {}), field_signature(fields[3], units[0], {})
        )

    def test_configuration_defaults_do_not_change_signatures(self):
        source = document('entity A { config attr x: decimal = 1 config attr y: decimal = 2.0 }')
        unit = resolve_scopes([source], [])[0]
        fields = children(children(source.tree, 'entity')[0], 'field')
        self.assertEqual(field_signature(fields[0], unit, {}), field_signature(fields[1], unit, {}))

    def test_scope_and_dependent_constraints_are_part_of_signature(self):
        sources = [
            document(
                'namespace a entity T {} entity A { attr selected: type<T> attr x: field_mapping<AttrCats.Static, self.selected::value> }'
            ),
            document(
                'namespace b entity T {} entity B { attr selected: type<T> attr x: field_mapping<AttrCats.Static, self.selected::value> }'
            ),
        ]
        units = resolve_scopes(sources, [])
        index = check_entities(units, [])
        signatures = [
            field_signature(index.fields[key]['x'].declaration, unit, index.field_types(key))
            for key, unit in zip([('a', 'A'), ('b', 'B')], units)
        ]
        self.assertNotEqual(*signatures)
        node = children(
            children(index.fields[('a', 'A')]['x'].declaration, 'field_body')[0], 'type_expr'
        )[0]
        self.assertNotEqual(
            type_signature(node, units[0], {}),
            type_signature(node, units[0], index.field_types(('a', 'A'))),
        )

    def test_dictionary_spelling_resolves_to_same_identity(self):
        unit = resolve_scopes(
            [document("decodes Codes {} entity A { attr x: code<Codes> attr y: code<'Codes'> }")],
            [],
        )[0]
        fields = children(children(unit.document.tree, 'entity')[0], 'field')
        self.assertEqual(field_signature(fields[0], unit, {}), field_signature(fields[1], unit, {}))
