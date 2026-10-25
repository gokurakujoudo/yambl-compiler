"""Reject corrupted semantic artifacts even when their digests have been recomputed."""

from dataclasses import replace
from unittest.mock import patch

from tests.support import FilesystemCase
from yambl_compiler.ir.application import compile_repository
from yambl_compiler.ir.codec import decode, encode, seal
from yambl_compiler.ir.model import Model
from yambl_compiler.ir.restoring import Restoring
from yambl_compiler.ir.validation import differences, validate_ir


class ValidationTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'main.yambl',
            """
namespace business
@targets(DecoratorTargets.Entity)
decorator @label(text: string = 'default')
@label
entity A { id key: string attr next: B attr price: decimal attr external: code<'Quotes'> }
entity B inherits A { @override attr price: exact<10.25> }
value item { attr name: string }
type alias_type = string
const FIXED: integer = 1
decodes Codes { - 1 First: 'First' }
""",
        )
        self.model = compile_repository(self.root)

    def corrupted(self, mutate, error):
        data = encode(self.model)
        mutate(data)
        model = seal(decode(data, Model))
        issues = validate_ir(model)
        self.assertTrue(issues, 'corruption passed independent checking')
        self.assertIn(error, '\n'.join(issues))
        self.assertTrue(all(issue.startswith('$') for issue in issues))

    def definition(self, data, name):
        return next(d for d in data['definitions'] if d['name'] == 'business.' + name)

    def test_catalogue_and_source_invariants(self):
        cases = [
            (lambda d: self.definition(d, 'FIXED').update(initializer=None), '.initializer'),
            (
                lambda d: self.definition(d, 'A').update(
                    references=[{'name': 'A', 'value': 'business.A'}]
                ),
                '.references',
            ),
            (
                lambda d: d['definitions'].append(d['definitions'][0]),
                'duplicate qualified',
            ),
            (lambda d: d.update(definitions=[]), 'incomplete embedded'),
            (lambda d: d.update(constants=[]), "eight result constants"),
            (lambda d: d.update(prefix='bad-prefix'), '$.prefix'),
            (lambda d: self.definition(d, 'A').update(identity='wrong'), '.identity'),
            (lambda d: self.definition(d, 'A').update(name='other.A'), '.name'),
            (
                lambda d: self.definition(d, 'A')['source'].update(namespace='a-b'),
                'lexical namespace',
            ),
            (
                lambda d: next(x for x in d['definitions'] if x['source']['origin'] == 'builtin')[
                    'source'
                ].update(namespace='other'),
                'require namespace builtins',
            ),
            (
                lambda d: self.definition(d, 'A')['source'].update(path='C:/secret/model.yambl'),
                'relative POSIX',
            ),
            (lambda d: self.definition(d, 'A')['source'].update(line=0), 'source span'),
            (
                lambda d: self.definition(d, 'A')['source'].update(end_line=1),
                'source span',
            ),
            (
                lambda d: self.definition(d, 'item').update(parent='business.A'),
                'only entities inherit',
            ),
            (
                lambda d: self.definition(d, 'A').update(
                    parameters=self.definition(d, 'label')['parameters']
                ),
                'unsupported declaration parameters',
            ),
            (
                lambda d: self.definition(d, 'alias_type').update(expression=None),
                'aliases and constants require a type expression',
            ),
            (
                lambda d: self.definition(d, 'Codes')['entries'][0].update(ordering='1.0'),
                'invalid integer',
            ),
        ]
        for mutate, error in cases:
            with self.subTest(error=error):
                self.corrupted(mutate, error)

    def test_expressions_and_scalar_payloads(self):
        cases = [
            (
                lambda d: self.definition(d, 'alias_type')['expression'].update(kind='literal'),
                'literal discriminator',
            ),
            (
                lambda d: self.definition(d, 'alias_type')['expression'].update(kind='reference'),
                'invalid resolved reference',
            ),
            (
                lambda d: self.definition(d, 'alias_type')['expression'].update(
                    kind='reference', target='absent.Type'
                ),
                'missing declaration',
            ),
            (
                lambda d: self.definition(d, 'label')['parameters'][0]['default']['literal'].update(
                    kind='type'
                ),
                'scalar literal',
            ),
            (
                lambda d: self.definition(d, 'A')['fields'][0]['expression']['arguments'][0].update(
                    target='Other'
                ),
                'external dictionary identity',
            ),
        ]
        for kind, text in [
            ('integer', '01'),
            ('decimal', 'NaN'),
            ('decimal', 'not numeric'),
            ('bool', 'True'),
        ]:
            cases.append(
                (
                    lambda d, kind=kind, text=text: self.definition(d, 'label')['parameters'][0][
                        'default'
                    ]['literal'].update(kind=kind, text=text),
                    'invalid scalar value',
                )
            )
        for mutate, error in cases:
            with self.subTest(error=error):
                self.corrupted(mutate, error)

    def test_broken_references_cycles_and_derived_views(self):
        cases = [
            (
                lambda d: d['fields'][0].update(applicable=[]),
                'inconsistent semantic value',
            ),
            (
                lambda d: self.definition(d, 'A').update(parent='business.Missing'),
                'missing reference',
            ),
            (lambda d: self.definition(d, 'A').update(parent='business.B'), 'cycle'),
            (
                lambda d: self.definition(d, 'alias_type')['expression'].update(
                    kind='reference', name='alias_type', target='business.alias_type'
                ),
                'cycle',
            ),
            (
                lambda d: self.definition(d, 'A')['effective'][0].update(
                    implementation='business.B.price'
                ),
                'inconsistent semantic value',
            ),
            (
                lambda d: self.definition(d, 'A')['fields'][0]['type'].update(name='wrong'),
                'inconsistent semantic value',
            ),
            (
                lambda d: self.definition(d, 'A')['decorators'][0]['bound'][0]['type'][
                    'exact'
                ].update(text='tampered'),
                'inconsistent semantic value',
            ),
            (
                lambda d: self.definition(d, 'A')['decorators'].append(
                    self.definition(d, 'A')['decorators'][0]
                ),
                'duplicate decorator',
            ),
            (
                lambda d: self.definition(d, 'B')['fields'][0].update(decorators=[]),
                'override',
            ),
            (
                lambda d: self.definition(d, 'Codes')['entries'][0].update(code='invalid_code'),
                'PascalCase',
            ),
            (lambda d: d.update(primitives=[]), 'inconsistent semantic value'),
            (
                lambda d: d.update(external_dictionaries=[]),
                'inconsistent semantic value',
            ),
        ]
        for mutate, error in cases:
            with self.subTest(error=error):
                self.corrupted(mutate, error)
        self.assertEqual(validate_ir(self.model), ())  # Entity references may be recursive.

    def test_validation_api_handles_wrong_dataclass_types_and_revision(self):
        self.assertIn('expected str', '\n'.join(validate_ir(replace(self.model, prefix=7))))
        self.assertIn('revision', '\n'.join(validate_ir(replace(self.model, format_revision=99))))
        self.assertIn(
            'digest mismatch',
            '\n'.join(validate_ir(replace(self.model, builtins_digest='x'))),
        )
        with patch(
            'yambl_compiler.ir.validation.restore',
            side_effect=RecursionError('nesting'),
        ):
            self.assertIn('nesting', '\n'.join(validate_ir(self.model)))
        self.assertEqual(differences({'a': 1}, {'b': 1}), ['$: inconsistent semantic value'])
        self.assertEqual(differences([1], [1, 2]), ['$: inconsistent semantic value'])

    def test_lexical_reference_conflict(self):
        adapter = Restoring()
        source = self.model.definitions[0].source
        adapter.reference('first.Item', source)
        with self.assertRaisesRegex(ValueError, 'conflicting lexical reference: Item'):
            adapter.reference('second.Item', source)
