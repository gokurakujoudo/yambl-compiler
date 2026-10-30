"""Verify portable constraint identities, source text, and independent validation."""

import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import document
from yambl_compiler.ir.codec import dumps, loads, seal
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.validation import validate_ir
from yambl_compiler.languages.yambl.semantics.validation import analyze


class ConstraintIRTests(unittest.TestCase):
    def lower(self, source):
        analysis = analyze([document(source)])
        self.assertEqual([d for d in analysis.diagnostics if d.severity == 'error'], [])
        return loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))

    def test_order_inheritance_source_and_tampering(self):
        source = '''entity A { id id: string attr n: integer constraint positive {
            let x = self.n::value()
            assert x > 0
            let y = x + 1
            assert y > x
        } }
        extend A { constraint extra { assert true } }
        entity B inherits A { constraint child { assert true } }
        '''
        model = self.lower(source)
        self.assertEqual(validate_ir(model), ())
        a = next(item for item in model.definitions if item.identity == 'A')
        b = next(item for item in model.definitions if item.identity == 'B')
        self.assertEqual(
            b.effective_constraints,
            ('A::constraint.extra', 'A::constraint.positive', 'B::constraint.child'),
        )
        constraint = next(item for item in a.constraints if item.name == 'positive')
        self.assertEqual(
            [s.kind for s in constraint.statements], ['let', 'assert', 'let', 'assert']
        )
        self.assertEqual(constraint.statements[1].assert_source, 'assert x > 0')
        changed = replace(b, effective_constraints=())
        invalid = seal(
            replace(
                model,
                definitions=tuple(changed if item is b else item for item in model.definitions),
            )
        )
        self.assertTrue(any('effective_constraints' in issue for issue in validate_ir(invalid)))
        formatted = self.lower(source.replace('assert x > 0', 'assert x   >   0'))
        self.assertEqual(model.model_digest, formatted.model_digest)
        self.assertNotEqual(
            constraint.statements[1].assert_source,
            next(
                item
                for item in next(d for d in formatted.definitions if d.identity == 'A').constraints
                if item.name == 'positive'
            )
            .statements[1]
            .assert_source,
        )

    def test_multiline_assertion_preserves_unicode_and_comments_without_changing_digest(self):
        source = '''entity A { id id: string attr n: integer constraint positive {
            assert (self.n::value() > 0  # 中文说明
                and true)
        } }'''
        model = self.lower(source)
        self.assertEqual(validate_ir(model), ())
        constraint = next(item for item in model.definitions if item.identity == 'A').constraints[0]
        self.assertIn('# 中文说明\n', constraint.statements[0].assert_source)
        edited = self.lower(source.replace('中文说明', '另一条注释'))
        self.assertEqual(model.model_digest, edited.model_digest)
        altered = self.lower(source.replace('> 0', '> 1'))
        self.assertNotEqual(model.model_digest, altered.model_digest)

    def test_extension_retains_its_lexical_scope(self):
        documents = [
            document('namespace common const LIMIT: integer = 2', 'common.yambl'),
            document('namespace core entity A { id id: string attr n: integer }', 'entity.yambl'),
            document(
                'namespace core from common import LIMIT extend A { constraint limit { assert self.n::value() < LIMIT } }',
                'extension.yambl',
            ),
        ]
        analysis = analyze(documents)
        self.assertEqual([d for d in analysis.diagnostics if d.severity == 'error'], [])
        model = loads(dumps(Lowering(analysis, Path('.'), '', 'test').model()))
        self.assertEqual(validate_ir(model), ())
        rule = next(item for item in model.definitions if item.identity == 'core.A').constraints[0]
        self.assertTrue(rule.extension)
        self.assertEqual(
            [(item.name, item.value) for item in rule.references], [('LIMIT', 'common.LIMIT')]
        )
