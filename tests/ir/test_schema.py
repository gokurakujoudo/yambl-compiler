"""Keep the published schema aligned with strict offline decoding."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.ir.codec import dumps, loads
from yambl_compiler.ir.schema import schema, validate_schema


class SchemaTests(FilesystemCase):
    def test_static_shorthand_normalizes_metadata_and_ir(self):
        from tests.support import document
        from yambl_compiler.semantics.validation import check

        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            "entity A { dimension dimension_11 { value source: string }  @dimension(dimension_11) attr label: string } entity B inherits A { dimension dimension_91 { value source: string }  @override @dimension(dimension_91) static attr label: string }",
        )
        model = compile_repository(self.root)
        fields = [f for d in model.definitions if d.source.origin == 'model' for f in d.fields]
        self.assertEqual([f.category for f in fields], ['static', 'static'])
        self.assertEqual(fields[0].identity, fields[1].identity)
        errors = check([document('entity A { attr keys: attrs<AttrCats.Attr, A> }')])
        self.assertTrue(any('Attr' in error.message for error in errors))

    def test_metaschema_publication_and_rejections(self):
        contract = schema()
        Draft202012Validator.check_schema(contract)
        published = Path(__file__).parents[3] / 'yambl/lang/syntex/ir.schema.json'
        self.assertEqual(json.loads(published.read_text(encoding='utf-8')), contract)
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', 'entity A { id key: string attr text: string }')
        model = compile_repository(self.root)
        text = dumps(model)
        self.assertEqual(text.count('\n'), 1)
        self.assertEqual(dumps(loads(text)), text)
        for mutate, path in [
            (lambda d: d.update(extra=True), '$'),
            (lambda d: d.update(prefix=1), '$.prefix'),
            (
                lambda d: d['definitions'][0].update(kind='execute'),
                '$.definitions[0].kind',
            ),
            (lambda d: d.update(format_revision=1), '$.format_revision'),
        ]:
            value = json.loads(text)
            mutate(value)
            with self.subTest(path=path), self.assertRaises(ValueError) as error:
                validate_schema(value)
            self.assertIn(path, str(error.exception))
        invalid = json.loads(text)
        invalid['prefix'] = False
        with self.assertRaisesRegex(ValueError, 'prefix'):
            loads(json.dumps(invalid))
