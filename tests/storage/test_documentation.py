"""Verify generated rST metadata, declaration provenance and Python contracts."""

import ast
import inspect
import io
from dataclasses import replace

from docutils import nodes
from docutils.core import publish_doctree

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.documentation import docstring, literal_section, metadata
from yambl_compiler.storage.generation import generate, python_package
from yambl_compiler.storage.layout import build_layout

SOURCE = "\n@title(text='根实体')\n@description('Root description')\nentity Root { dimension dimension_150 { value source: code<'Sources'> hierarchy priority: Hierarchy } dimension dimension_211 { value venue: string } \n    id key: string\n    @title('Price')\n    @description(text='Observed price')\n    @dimension(dimension_150)\n    @dimension(dimension_211)\n    @temporal(day: date)\n    snapshot attr price: decimal\n    scd attr balance: decimal\n    config attr limit: integer = 20\n    attr tag: string\n}\n@title('Zulu title')\nentity Zulu inherits Root { dimension dimension_545 { value source: code<'Sources'> hierarchy priority: Hierarchy } dimension dimension_606 { value venue: string } \n    @overload attr shared: string\n    @override\n    @title(text='Price')\n    @description('Observed price')\n    @dimension(dimension_545)\n    @dimension(dimension_606)\n    @temporal(day: date)\n    snapshot attr price: decimal\n}\nentity Alpha inherits Root {\n    @overload attr shared: string\n    @override attr tag: exact<'fixed'>\n}\nentity Child inherits Alpha {}\nextend Root { attr extended: string }\n"


class DocumentationTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write("yambl-root.lclcfg", "prefix: 'sample'\n")
        self.write("main.yambl", SOURCE)
        self.layout = build_layout(compile_repository(self.root))
        self.source = python_package(self.layout, "sample_model")
        self.tree = ast.parse(self.source)
        self.classes = {
            node.name: node for node in self.tree.body if isinstance(node, ast.ClassDef)
        }

    def method(self, name):
        return next(
            node
            for node in self.classes["Root"].body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    def test_metadata_declaration_owners_and_dimension_contracts(self):
        root = ast.get_docstring(self.classes["Root"])
        self.assertIn("Title::\n\n    根实体", root)
        self.assertIn("Description::\n\n    Root description", root)
        self.assertIn("Zulu title", ast.get_docstring(self.classes["Zulu"]))
        self.assertNotIn("Title::", ast.get_docstring(self.classes["Child"]))
        for name in ("read_price", "read_price_range"):
            doc = ast.get_docstring(self.method(name))
            self.assertIn("Title::\n\n    Price", doc)
            self.assertIn("Description::\n\n    Observed price", doc)
            self.assertIn("Declaring entities::\n\n    sample.Root\n    sample.Zulu", doc)
            self.assertIn("source: code<sample.Sources> (required explicitly)", doc)
            self.assertIn(
                "Hierarchy selector priority: builtins.Hierarchy (used by best-available selection; exact reads do not apply it)",
                doc,
            )
            self.assertLess(doc.index("source: code"), doc.index("venue: string"))
            self.assertIn(":param source:", doc)
            self.assertIn(":param venue:", doc)
            self.assertNotIn(":param priority:", doc)
        point = ast.get_docstring(self.method("read_price"))
        self.assertIn(":param day: Exact snapshot date.", point)
        self.assertIn(":rtype: ReadResult[Decimal]", point)
        self.assertIn(
            "Existing snapshot dates only.",
            ast.get_docstring(self.method("read_price_range")),
        )
        self.assertIn("State at the start", ast.get_docstring(self.method("read_balance_range")))
        self.assertIn("valid_from, valid_to", ast.get_docstring(self.method("read_balance")))
        self.assertIn("origin model_default", ast.get_docstring(self.method("read_limit")))
        self.assertIn(
            "Declaring entities::\n\n    sample.Alpha\n    sample.Zulu",
            ast.get_docstring(self.method("read_shared")),
        )
        self.assertNotIn("sample.Child", ast.get_docstring(self.method("read_shared")))
        self.assertIn(
            "Declaring entities::\n\n    sample.Root",
            ast.get_docstring(self.method("read_extended")),
        )
        self.assertNotIn("Dimensions::", ast.get_docstring(self.method("read_extended")))
        self.assertIn("YAMBL type::\n\n    string\n", ast.get_docstring(self.method("read_tag")))
        self.assertNotIn("fixed", ast.get_docstring(self.method("read_tag")))
        self.assertEqual(python_package(self.layout, "sample_model"), self.source)

    def test_every_generated_docstring_is_valid_rst_and_covers_parameters(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                continue
            with self.subTest(name=getattr(node, "name", "<module>")):
                doc = ast.get_docstring(node)
                self.assertTrue(doc)
                parsed = publish_doctree(
                    doc,
                    settings_overrides={
                        "warning_stream": io.StringIO(),
                        "halt_level": 6,
                        "raw_enabled": False,
                        "file_insertion_enabled": False,
                    },
                )
                self.assertEqual(list(parsed.findall(nodes.system_message)), [])
                if isinstance(node, ast.FunctionDef):
                    fields = [(f[0].astext(), f[1].astext()) for f in parsed.findall(nodes.field)]
                    self.assertTrue(all(body.strip() for _, body in fields))
                    self.assertCountEqual(
                        [
                            name.removeprefix("param ")
                            for name, _ in fields
                            if name.startswith("param ")
                        ],
                        [
                            a.arg
                            for a in (*node.args.args, *node.args.kwonlyargs)
                            if a.arg != "self"
                        ],
                    )
                    self.assertIn("returns", [name for name, _ in fields])
                    self.assertEqual(dict(fields)["rtype"], ast.unparse(node.returns))
        generate(self.layout.model, self.root / "gen", "sample_model")
        namespace = vars(load_generated(self.root / "gen/sample_model", "sample_model"))
        self.assertEqual(
            inspect.getdoc(namespace["Root"].read_price),
            ast.get_docstring(self.method("read_price")),
        )

    def test_literal_model_prose_cannot_change_python_or_rst_structure(self):
        hostile = (
            '中文 """ C:\\data\\file\n\n.. include:: private.txt\n\n:type fake: *literal* `text`'
        )
        source = "\n".join(
            [
                "class Example:",
                *docstring(
                    [
                        "Model description.",
                        "",
                        *literal_section("Description", hostile),
                    ],
                    4,
                ),
            ]
        )
        tree = ast.parse(source)
        namespace = {}
        exec(compile(tree, "<generated>", "exec"), namespace)
        parsed = publish_doctree(
            ast.get_docstring(tree.body[0]),
            settings_overrides={"warning_stream": io.StringIO()},
        )
        self.assertEqual(list(parsed.findall(nodes.system_message)), [])
        self.assertEqual(list(parsed.findall(nodes.field)), [])
        self.assertEqual(
            [block.astext() for block in parsed.findall(nodes.literal_block)], [hostile]
        )
        self.assertIn('"""', namespace["Example"].__doc__)
        self.assertIn("C:\\data\\file", namespace["Example"].__doc__)
        call = self.layout.entities["sample.Root"].decorators[0]
        bound = call.bound[0]
        for exact in (
            None,
            replace(bound.type.exact, text=""),
            replace(bound.type.exact, text="   "),
        ):
            changed = replace(call, bound=(replace(bound, type=replace(bound.type, exact=exact)),))
            self.assertEqual(metadata((changed,)), [])
        self.assertIn("\\r", "\n".join(docstring(["A\rB"], 0)))
        nul_source = "\n".join(docstring(["A\x00B"], 0))
        self.assertNotIn("\x00", nul_source)
        self.assertEqual(ast.get_docstring(ast.parse(nul_source)), "A\x00B")
