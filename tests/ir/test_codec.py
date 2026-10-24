"""Exercise strict JSON decoding and content identity."""

from dataclasses import replace

from tests.support import FilesystemCase
from yambl_compiler.ir.application import compile_repository
from yambl_compiler.ir.codec import decode, dumps, encode, loads, seal


class CodecTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'main.yambl', 'entity A { config attr price: decimal = 12345678901234567890.125 }'
        )
        self.model = compile_repository(self.root)

    def test_strict_json_and_revision(self):
        cases = [
            ('{', 'Expecting'),
            ('[]', 'revision'),
            ('{"format_revision": true}', 'revision'),
            ('{"format_revision": 99}', 'recompile'),
            ('{"format_revision":1,"format_revision":1}', 'duplicate JSON key'),
            ('{"format_revision":1,"unknown":NaN}', 'nonstandard'),
            ('{"format_revision":1,"unknown":Infinity}', 'nonstandard'),
            ('{"format_revision":1,"unknown":-Infinity}', 'nonstandard'),
            ('\ufeff' + dumps(self.model), 'BOM'),
        ]
        for text, error in cases:
            with self.subTest(text=text[:80]), self.assertRaisesRegex(ValueError, error):
                loads(text)

    def test_every_wire_shape_is_strict(self):
        cases = [
            (lambda d: d.update(extra=True), 'unknown fields'),
            (lambda d: d.pop('prefix'), 'missing fields'),
            (lambda d: d.update(prefix=1), r'\$\.prefix'),
            (lambda d: d.update(definitions={}), 'expected an array'),
            (lambda d: d.update(definitions=[False]), 'expected an object'),
            (lambda d: d['definitions'][0].update(kind='execute'), 'unsupported discriminator'),
            (lambda d: d['definitions'][0].update(kind=1), 'unsupported discriminator'),
            (lambda d: d['definitions'][0].update(final=1), 'expected bool'),
            (lambda d: d['definitions'][0]['source'].update(line=True), 'expected int'),
            (lambda d: d['definitions'][0].update(expression=0), 'expected an object'),
            (
                lambda d: d['definitions'][0]['fields'][0]['type'].update(dependent=1.25),
                'expected bool',
            ),
        ]
        for mutate, error in cases:
            data = encode(self.model)
            mutate(data)
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                decode(data, type(self.model))

    def test_numbers_and_output_bytes_preserve_precision(self):
        text = dumps(self.model)
        self.assertIn('"text":"12345678901234567890.125"', text)
        self.assertEqual(loads(text), self.model)
        self.assertTrue(text.endswith('\n'))
        self.assertFalse(text.endswith('\n\n'))
        self.assertNotIn('\r', text)
        changed = seal(replace(self.model, compiler_version='future'))
        self.assertEqual(changed.model_digest, self.model.model_digest)
        self.assertEqual(changed.builtins_digest, self.model.builtins_digest)
        self.assertNotEqual(dumps(changed), text)
