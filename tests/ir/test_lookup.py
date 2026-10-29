"""Round-trip named lookup contracts without access to model source files."""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import document
from yambl_compiler.ir import compile_repository
from yambl_compiler.ir.codec import dumps, loads, seal
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.model import Text
from yambl_compiler.ir.validation import validate_ir
from yambl_compiler.languages.yambl.semantics.validation import analyze

FMC_ROOT = Path(__file__).resolve().parents[3] / 'fmc-data-model' / 'model'


class LookupIRTests(unittest.TestCase):
    @unittest.skipUnless(FMC_ROOT.is_dir(), 'FMC model repository is not present')
    def test_fmc_security_lookup_contracts(self):
        model = compile_repository(FMC_ROOT)
        entity = next(d for d in model.definitions if d.identity == 'core.Security')
        actual = {
            rule.name: (
                [
                    (key.name, key.type.name, tuple(arg.target for arg in key.type.arguments))
                    for key in rule.keys
                ],
                [
                    (item.name, item.type.name, tuple(arg.target for arg in item.type.arguments))
                    for item in rule.inputs
                ],
            )
            for rule in entity.lookups
        }
        self.assertEqual(
            actual,
            {
                'by_name': (
                    [
                        ('security_name', 'string', ()),
                        ('market', 'code', ('core.TradingMarkets',)),
                        ('issuer_name', 'string', ()),
                    ],
                    [],
                ),
                'by_client_id': (
                    [('client_id', 'string', ())],
                    [
                        ('as_of_date', 'date', ()),
                        ('client_id_type', 'code', ('core.ClientIdTypes',)),
                    ],
                ),
                'by_wind_id': ([('client_id', 'string', ())], [('as_of_date', 'date', ())]),
                'by_issuer_industry': (
                    [('issuer_name', 'string', ()), ('industry', 'string', ())],
                    [
                        ('as_of_date', 'date', ()),
                        ('classification', 'code', ('decodes.IndustryTypes',)),
                        ('industry_date', 'date', ()),
                    ],
                ),
            },
        )

    def model(self, extension):
        sources = [
            "namespace codes decodes Sources { - 1 Wind: 'Wind' }",
            "namespace core from codes import Sources entity E { id key: string dimension d { value source: code<Sources> } @dimension(d) @temporal(day: date) scd attr client: string } entity Child inherits E {}",
            "namespace core from codes import Sources const DAY: date = parse_date('2026-10-28') "
            + extension,
        ]
        analysis = analyze([document(source, f'{i}.yambl') for i, source in enumerate(sources)])
        self.assertEqual([d.message for d in analysis.diagnostics], [])
        return Lowering(analysis, Path('.'), '', 'test').model()

    def test_roundtrip_inheritance_bindings_and_source_independence(self):
        model = self.model(
            'extend E { lookup by_client { client_id = self.client::value(day=DAY, source=Sources.Wind) } }'
        )
        restored = loads(dumps(model))
        self.assertEqual(validate_ir(restored), ())
        entity = next(d for d in restored.definitions if d.identity == 'core.E')
        child = next(d for d in restored.definitions if d.identity == 'core.Child')
        rule = entity.lookups[0]
        self.assertTrue(rule.extension)
        self.assertEqual(rule.source.path, '2.yambl')
        self.assertEqual(
            [(r.name, r.value) for r in child.effective_lookups], [('by_client', 'core.E')]
        )
        self.assertEqual([k.name for k in rule.keys], ['client_id'])
        bindings = rule.keys[0].steps[0].bindings
        self.assertEqual(
            [(b.name, b.fixed_kind, b.fixed_text, b.fixed_target) for b in bindings],
            [('day', 'date', '2026-10-28', ''), ('source', 'code', 'Wind', 'codes.Sources')],
        )
        self.assertNotIn(str(Path.cwd()), dumps(model))

    def test_entity_and_same_file_extension_keep_distinct_declaration_kinds(self):
        source = '''namespace core
entity E {
    id key: string
    lookup by_key { key = self.key::value() }
}
extend E { lookup by_extension { key = self.key::value() } }
'''
        analysis = analyze([document(source, 'one.yambl')])
        self.assertEqual([d.message for d in analysis.diagnostics], [])
        model = Lowering(analysis, Path('.'), '', 'test').model()
        self.assertEqual(validate_ir(loads(dumps(model))), ())
        entity = next(d for d in model.definitions if d.identity == 'core.E')
        self.assertEqual(
            [(rule.name, rule.extension, rule.source.line) for rule in entity.lookups],
            [('by_extension', True, 6), ('by_key', False, 4)],
        )

    def test_semantic_digest_and_tampering(self):
        source = '''entity E {
            id id: string
            attr first: string
            attr second: string
            lookup by_value { value = self.first::value() }
        }'''
        paths = []
        for text in (source, source.replace('self.first::value()', 'self.second::value()')):
            analysis = analyze([document(text, 'lookup.yambl')])
            self.assertEqual(analysis.diagnostics, ())
            paths.append(loads(dumps(Lowering(analysis, Path('.'), '', 'test').model())))
        self.assertNotEqual(paths[0].model_digest, paths[1].model_digest)
        first = self.model(
            'extend E { lookup by_client { client_id = self.client::value(day=input.date, source=Sources.Wind) } }'
        )
        changed = self.model(
            'extend E { lookup by_client { client_id = self.client::value(day=input.other_date, source=Sources.Wind) } }'
        )
        moved = self.model(
            '\n\n# comment\nextend E { lookup by_client { client_id = self.client::value(day=input.date, source=Sources.Wind) } }'
        )
        self.assertNotEqual(first.model_digest, changed.model_digest)
        self.assertEqual(first.model_digest, moved.model_digest)
        entity = next(d for d in first.definitions if d.identity == 'core.E')
        rule = entity.lookups[0]
        key = rule.keys[0]
        step = key.steps[0]
        for altered in (
            replace(step, access=replace(step.access, identity='core.E.missing')),
            replace(step, access=replace(step.access, implementation='core.E.missing')),
            replace(
                step, bindings=(replace(step.bindings[0], input_name='wrong'), *step.bindings[1:])
            ),
            replace(
                step,
                bindings=(
                    *step.bindings[:1],
                    replace(step.bindings[1], fixed_target='codes.Other'),
                ),
            ),
        ):
            with self.subTest(altered=altered):
                changed_entity = replace(
                    entity, lookups=(replace(rule, keys=(replace(key, steps=(altered,)),)),)
                )
                forged = seal(
                    replace(
                        first,
                        definitions=tuple(
                            changed_entity if d.identity == entity.identity else d
                            for d in first.definitions
                        ),
                    )
                )
                self.assertTrue(validate_ir(forged))
        wire = json.loads(dumps(first))
        wire['format_revision'] = 9
        with self.assertRaisesRegex(ValueError, 'unsupported IR revision'):
            loads(json.dumps(wire))

    def test_invalid_type_bindings_and_lexical_reference_are_rejected(self):
        model = self.model(
            'extend E { lookup by_client { client_id = self.client::value(day=input.date, source=Sources.Wind) } }'
        )
        entity = next(d for d in model.definitions if d.identity == 'core.E')
        rule = entity.lookups[0]
        key = rule.keys[0]
        step = key.steps[0]
        cases = [
            (
                replace(rule, keys=(replace(key, type=replace(key.type, name='integer')),)),
                '.type.name: inconsistent semantic value',
            ),
            (
                replace(
                    rule, keys=(replace(key, steps=(replace(step, bindings=step.bindings[:-1]),)),)
                ),
                '.bindings: inconsistent semantic value',
            ),
            (
                replace(
                    rule,
                    keys=(
                        replace(
                            key,
                            steps=(
                                replace(
                                    step,
                                    bindings=(
                                        step.bindings[0],
                                        replace(step.bindings[1], input_name='source'),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                'expected an input, fixed value, or expression',
            ),
            (
                replace(
                    rule,
                    keys=(
                        replace(
                            key,
                            steps=(
                                replace(
                                    step, access=replace(step.access, operation='best_available')
                                ),
                            ),
                        ),
                    ),
                ),
                '.access.operation: inconsistent semantic value',
            ),
            (
                replace(rule, references=(Text('Sources', 'codes.Missing'),)),
                'missing reference',
            ),
            (
                replace(
                    rule,
                    keys=(
                        replace(
                            key,
                            steps=(
                                replace(
                                    step,
                                    bindings=(
                                        replace(step.bindings[0], fixed_target='codes.Sources'),
                                        step.bindings[1],
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                'only codes have a dictionary target',
            ),
            (
                replace(
                    rule,
                    keys=(
                        replace(
                            key,
                            steps=(
                                replace(
                                    step,
                                    bindings=(
                                        step.bindings[0],
                                        replace(step.bindings[1], fixed_target=''),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                'code requires a dictionary target',
            ),
        ]
        for altered, expected in cases:
            with self.subTest(expected=expected):
                changed = replace(entity, lookups=(altered,))
                forged = seal(
                    replace(
                        model,
                        definitions=tuple(
                            changed if d.identity == entity.identity else d
                            for d in model.definitions
                        ),
                    )
                )
                self.assertTrue(any(expected in message for message in validate_ir(forged)))

        child = next(d for d in model.definitions if d.identity == 'core.Child')
        changed_child = replace(child, effective_lookups=(Text('by_client', 'core.Child'),))
        forged = seal(
            replace(
                model,
                definitions=tuple(
                    changed_child if d.identity == child.identity else d for d in model.definitions
                ),
            )
        )
        self.assertTrue(any('.effective_lookups' in message for message in validate_ir(forged)))

        wire = json.loads(dumps(model))
        wire_entity = next(d for d in wire['definitions'] if d['identity'] == 'core.E')
        wire_entity['lookups'][0]['keys'][0]['steps'][0]['bindings'][1]['fixed_kind'] = 4
        with self.assertRaisesRegex(ValueError, 'fixed_kind'):
            loads(json.dumps(wire))
