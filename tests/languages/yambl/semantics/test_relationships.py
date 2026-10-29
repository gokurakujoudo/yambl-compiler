"""Check relationship cardinality and matching declarations."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.formatting.formatter import format_document
from yambl_compiler.languages.yambl.semantics.validation import analyze


class RelationshipTests(unittest.TestCase):
    def check_source(self, source):
        return [item.message for item in analyze([document(source)]).diagnostics]

    def test_valid_cardinalities_and_formatting(self):
        for x in ('one', 'many'):
            for y, forward in (
                ('maybe', 'Target'),
                ('one', 'Target'),
                ('maybe', 'collection<Target>'),
                ('many', 'collection<Target>'),
            ):
                reverse = 'Source' if x == 'one' else 'collection<Source>'
                source = f'''entity Source {{ id id:string @relationship({x}, {y}) attr target:{forward} }}
                    entity Target {{ id id:string @reverse_relationship(Source.target) attr sources:{reverse} }}'''
                with self.subTest(x=x, y=y, forward=forward):
                    self.assertEqual(self.check_source(source), [])
                    formatted = format_document(document(source))
                    self.assertEqual(format_document(document(formatted)), formatted)

    def test_invalid_cardinality_and_targets(self):
        for expression, type_, message in (
            ('maybe, one', 'Target', 'invalid relationship cardinality: x'),
            ('one, unknown', 'Target', 'invalid relationship cardinality: y'),
            ("one, 'one'", 'Target', 'invalid relationship cardinality: y'),
            ('one, many', 'Target', 'relationship cardinality does not match field type'),
            (
                'one, one',
                'collection<Target>',
                'relationship cardinality does not match field type',
            ),
            (
                'one, one',
                'string',
                'relationship requires an entity reference or collection of entity references',
            ),
        ):
            with self.subTest(expression=expression, type=type_):
                source = f'entity Target {{ id id:string }} entity Source {{ id id:string @relationship({expression}) attr field:{type_} }}'
                self.assertIn(message, self.check_source(source))

        missing = 'entity Target { id id:string @reverse_relationship attr sources:Target }'
        self.assertIn('missing decorator argument: target', self.check_source(missing))
        invalid = (
            "entity Target { id id:string @reverse_relationship('invalid') attr sources:Target }"
        )
        self.assertIn(
            'reverse relationship requires an entity field reference', self.check_source(invalid)
        )
        unknown = 'extend Missing { @relationship(one,one) attr target:Missing }'
        self.assertTrue(self.check_source(unknown))

    def test_reverse_mismatches_and_missing_target(self):
        source = '''entity Source { id id:string @relationship(many,one) attr target:Target attr plain: Target }
            entity Target { id id:string @reverse_relationship(Source.target) attr sources:collection<Source> }'''
        for edited, message in (
            (
                source.replace('collection<Source>', 'Source'),
                'reverse relationship type does not match source cardinality',
            ),
            (
                source.replace('Source.target', 'Source.plain'),
                'reverse relationship target must be a forward relationship field',
            ),
            (
                source.replace('Source.target', 'Source.missing'),
                'reverse relationship target must be a forward relationship field',
            ),
            (
                source.replace('Source.target', 'Target.sources'),
                'reverse relationship target must be a forward relationship field',
            ),
            (
                source.replace('attr target:Target', 'attr target:Source'),
                'forward relationship target must match reverse owner',
            ),
            (
                source.replace(
                    '@reverse_relationship(Source.target)',
                    '@sparse(\'x\') @reverse_relationship(Source.target)',
                ),
                'reverse relationship cannot have forward or storage layout annotations',
            ),
        ):
            with self.subTest(message=message):
                self.assertIn(message, self.check_source(edited))

    def test_dimensions_and_temporal_parameters(self):
        source = '''dimension d { value source:string }
            entity Source { id id:string @dimension(d) @temporal(day:date) @relationship(many,one) snapshot attr target:Target }
            entity Target { id id:string @dimension(d) @temporal(day:date) @reverse_relationship(Source.target) snapshot attr sources:collection<Source> }'''
        self.assertEqual(self.check_source(source), [])
        wrong_dimension = source.replace(
            '@dimension(d) @temporal(day:date) @reverse', '@temporal(day:date) @reverse'
        )
        self.assertIn(
            'relationship dimensions must match by declaration identity',
            self.check_source(wrong_dimension),
        )
        wrong_time = source.replace(
            '@temporal(day:date) @reverse', '@temporal(as_of:date) @reverse'
        )
        self.assertIn(
            'relationship categories and temporal parameters must match',
            self.check_source(wrong_time),
        )
        hierarchy = source.replace(
            'value source:string }', 'value source:string hierarchy h:Hierarchy }'
        )
        self.assertIn(
            'relationship dimensions cannot have hierarchy or fallback',
            self.check_source(hierarchy),
        )

    def test_imports_inheritance_and_named_arguments(self):
        source = document(
            '''namespace source
            entity Source { id id:string @relationship(y=one, x=many) attr target:Target }
            entity Child inherits Source {}
            entity Target { id id:string }
        ''',
            'source.yambl',
        )
        extension = document(
            '''namespace use
            from source import Child
            from source import Target
            extend Target { @reverse_relationship(target=Child.target) attr sources:collection<Child> }
        ''',
            'extension.yamambl',
        )
        analysis = analyze([source, extension])
        self.assertEqual([item.message for item in analysis.diagnostics], [])

    def test_distinct_dimensions_and_category_mismatch(self):
        source = '''dimension a { value source:string }
            dimension b { value source:string }
            entity Source { id id:string @dimension(a) @relationship(many,one) attr target:Target }
            entity Target { id id:string @dimension(b) @reverse_relationship(Source.target) attr sources:collection<Source> }'''
        self.assertIn(
            'relationship dimensions must match by declaration identity', self.check_source(source)
        )
        timed = source.replace(
            '@dimension(b) @reverse', '@dimension(a) @temporal(day:date) @reverse'
        ).replace('attr sources:collection<Source>', 'snapshot attr sources:collection<Source>')
        self.assertIn(
            'relationship categories and temporal parameters must match', self.check_source(timed)
        )


if __name__ == '__main__':
    unittest.main()
