"""Reject malformed portable contracts before opening any data backend."""

import hashlib
import json
from typing import Literal

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.contract import decode, dumps, load_contract, loads
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.records import Column


class ContractTests(FilesystemCase):
    def test_roundtrip_and_strict_shape_checks(self):
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write("main.yambl", "entity A { id key: string attr x: string }")
        layout = build_layout(compile_repository(self.root))
        path = self.write("runtime.json", dumps(layout))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(load_contract(path, digest), layout)
        with self.assertRaisesRegex(ValueError, "digest"):
            load_contract(path, "wrong")
        for value in (
            [],
            {},
            {"runtime_revision": True, "layout": {}},
            {"runtime_revision": 9, "layout": {}},
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
            ({}, object),
            ({}, Column),
        ):
            with self.subTest(value=value, type_=type_), self.assertRaises(ValueError):
                decode(value, type_)
        self.assertEqual(decode("ok", Literal["ok"]), "ok")
        self.assertEqual(decode([1, "s"], tuple[int, str]), (1, "s"))
        self.assertEqual(decode([True], list[bool]), [True])
        self.assertEqual(decode(None, str | None), None)
        self.assertEqual(decode("x", str | None), "x")
