"""Keep application docstrings complete and valid without importing source modules."""

import ast
import io
import re
import unittest
from pathlib import Path

from docutils import nodes
from docutils.core import publish_doctree


class DocumentationTests(unittest.TestCase):
    def test_rst_docstrings_and_parameters(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(
                    node,
                    (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    continue
                with self.subTest(path=path.name, name=getattr(node, 'name', '<module>')):
                    doc = ast.get_docstring(node)
                    self.assertTrue(doc, 'missing docstring')
                    rendered = publish_doctree(
                        doc,
                        settings_overrides={
                            'halt_level': 6,
                            'report_level': 2,
                            'warning_stream': io.StringIO(),
                            'file_insertion_enabled': False,
                            'raw_enabled': False,
                        },
                    )
                    self.assertEqual(
                        [
                            message.astext()
                            for message in rendered.findall(nodes.system_message)
                            if message['level'] >= 2
                        ],
                        [],
                    )
                    fields = [
                        (field[0].astext(), field[1].astext().strip())
                        for field in rendered.findall(nodes.field)
                    ]
                    actual = [
                        name.removeprefix('param ')
                        for name, body in fields
                        if name.startswith('param ') and body
                    ]
                    expected = []
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        expected = [
                            arg.arg
                            for arg in (
                                *node.args.posonlyargs,
                                *node.args.args,
                                *node.args.kwonlyargs,
                            )
                            if arg.arg not in {'self', 'cls'}
                        ]
                        for arg in (node.args.vararg, node.args.kwarg):
                            if arg:
                                expected.append(arg.arg)
                        self.assertTrue(
                            any(name in {'returns', 'yields'} and body for name, body in fields)
                        )
                    elif isinstance(node, ast.ClassDef) and any(
                        re.search(r'\bdataclass\b', ast.unparse(decorator))
                        for decorator in node.decorator_list
                    ):
                        expected = [
                            item.target.id
                            for item in node.body
                            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                        ]
                        class_fields = [
                            item.target.id
                            for item in node.body
                            if isinstance(item, ast.AnnAssign)
                            and isinstance(item.target, ast.Name)
                            and isinstance(item.annotation, ast.Subscript)
                            and ast.unparse(item.annotation.value) == 'ClassVar'
                        ]
                        for name in class_fields:
                            description = next(
                                (body for field, body in fields if field == f'param {name}'), ''
                            ).lower()
                            self.assertIn('class constant', description)
                            self.assertIn('not a constructor parameter', description)
                    self.assertCountEqual(actual, expected)
