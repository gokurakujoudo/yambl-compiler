"""Shared source builders and isolated filesystem fixtures."""

import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from yambl_compiler.languages.yambl.syntax.parser import Diagnostic, Document, parse


def load_generated(path, name):
    """Test generated bindings against maintained runtime sources.

    Separate isolated-process tests exercise the distributed copies without aliases.
    """
    spec = importlib.util.spec_from_file_location(name, path / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name + "._calculations", None)
    sys.modules.pop(name + '._value_types', None)
    sys.modules[name] = module
    for part in (
        "records",
        "contract",
        "backends",
        "batch",
        "configuration",
        "arithmetic",
        'conversions',
        "selection",
        "lookup",
        "lookup_prepare",
        "computing",
        "session",
        "results",
        "runtime",
        "validation",
    ):
        sys.modules[name + "._" + part] = importlib.import_module("yambl_compiler.storage." + part)
    sys.modules[name + "._ir_model"] = importlib.import_module("yambl_compiler.ir.model")
    sys.modules[name + '.transactions'] = importlib.import_module('yambl_compiler.transactions')
    for part in (
        'writing',
        'binding',
        'editing',
        'execution',
        'snapshot',
        'repository',
    ):
        sys.modules[name + '.transactions.' + part] = importlib.import_module(
            'yambl_compiler.transactions.' + part
        )
    spec.loader.exec_module(module)
    return module


def document(text: str, name: str = "model.yambl") -> Document:
    result = parse(text, Path(name))
    if isinstance(result, Diagnostic):
        raise AssertionError(str(result))
    return result


class FilesystemCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path
