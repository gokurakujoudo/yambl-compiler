"""Shared source builders and isolated filesystem fixtures."""

import tempfile
import unittest
from pathlib import Path

from yambl_compiler.syntax.parser import Diagnostic, Document, parse


def document(text: str, name: str = 'model.yambl') -> Document:
    result = parse(text, Path(name))
    if isinstance(result, Diagnostic):
        raise AssertionError(str(result))
    return result


class FilesystemCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode('utf-8'))
        return path
