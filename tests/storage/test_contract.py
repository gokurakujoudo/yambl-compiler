"""Reject malformed portable contracts before opening any data backend."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from tests.support import FilesystemCase, document
from yambl_compiler.ir import compile_repository
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.languages.yambl.semantics.validation import analyze
from yambl_compiler.storage.contract import decode, dumps, load_contract, loads
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.records import LAYOUT_REVISION, Column


class ContractTests(FilesystemCase):
    def test_roundtrip_and_strict_shape_checks(self):
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write("main.yambl", "entity A { id key: string attr x: string }")
        layout = build_layout(compile_repository(self.root))
        path = self.write("runtime.json", dumps(layout))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(load_contract(path, digest), layout)
        wire = json.loads(dumps(layout))
        self.assertEqual(wire['runtime_revision'], 19)
        self.assertEqual(LAYOUT_REVISION, 9)
        wire['runtime_revision'] = 11
        path.write_text(json.dumps(wire), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unsupported runtime contract revision'):
            load_contract(path, hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, "digest"):
            load_contract(path, "wrong")
        wire = json.loads(dumps(layout))
        wire['layout']['model'] = None
        path.write_text(json.dumps(wire), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'expected object'):
            load_contract(path, hashlib.sha256(path.read_bytes()).hexdigest())
        for value in (
            [],
            {},
            {"runtime_revision": True, "layout": {}},
            {"runtime_revision": 10, "layout": {}},
            {"runtime_revision": 5, "layout": {}},
        ):
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "revision"):
                load_contract(path, hashlib.sha256(path.read_bytes()).hexdigest())
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1.1}', "broken"):
            with self.assertRaises(ValueError):
                loads(text)
        for value, type_ in (
            (1, str),
            ("bad", Literal["ok"]),
            ({}, list[str]),
            ([1], tuple[int, str]),
            ([], dict[str, int]),
            ({}, Column),
        ):
            with self.subTest(value=value, type_=type_), self.assertRaises(ValueError):
                decode(value, type_)
        self.assertEqual(decode("ok", Literal["ok"]), "ok")
        self.assertEqual(decode([1, "s"], tuple[int, str]), (1, "s"))
        self.assertEqual(decode([True], list[bool]), [True])
        self.assertEqual(decode(None, str | None), None)
        self.assertEqual(decode("x", str | None), "x")
        self.assertEqual(decode(7, str | int), 7)
        with self.assertRaisesRegex(ValueError, 'invalid union value'):
            decode('invalid', int | None)

    def test_lookup_contract_roundtrip_without_sources(self):
        source = "entity E { id id: string attr name: string lookup by_name { name = self.name::value() } }"
        analysis = analyze([document(source)])
        self.assertEqual([d.message for d in analysis.diagnostics], [])
        model = Lowering(analysis, Path('.'), '', 'test').model()
        layout = build_layout(model)
        path = self.write('lookup-runtime.json', dumps(layout))
        restored = load_contract(path, hashlib.sha256(path.read_bytes()).hexdigest())
        rule = restored.entities['E'].lookups[0]
        self.assertEqual(rule.name, 'by_name')
        self.assertEqual(
            (rule.keys[0].name, rule.keys[0].type.name, rule.keys[0].steps[0].access.identity),
            ('name', 'string', 'E.name'),
        )
        self.assertEqual(restored.model.model_digest, model.model_digest)
