"""Validate cycle scope by logical field, dimensions and time intersection."""

import asyncio
import contextlib
import io
import json
import sqlite3
from dataclasses import asdict
from unittest.mock import patch

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status
from yambl_compiler.storage.validation import cycles
from yambl_compiler.transactions.cli import main
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.writing import dumps

SOURCE = "entity Node { dimension dimension_207 { value source: string } \n id key: string\n attr parent: Node\n attr other: Node\n attr foreign: Other\n attr narrow: Child\n attr children: collection<Node>\n config attr limit: integer = 1\n config attr active: bool = true\n @dimension(dimension_207) attr dim: Node\n snapshot attr snap: Node\n scd attr period: Node\n}\nentity Child inherits Node { @override attr parent: Node attr extra: string }\nentity Other { id key: string attr parent: Node }\n"


class ValidationTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write("main.yambl", SOURCE)
        self.output = self.root / "generated"
        self.layout = generate(compile_repository(self.root), self.output, "sample_model")
        self.api = load_generated(self.output / "sample_model", "sample_model")
        self.path = self.root / "data.db"
        with asyncio.run(self.api.SampleData.create(self.config('sqlite', self.path))):
            pass
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executemany(
            "INSERT INTO tb_node(id, entity_type) VALUES (?, ?)",
            [("a", "Node"), ("b", "Child"), ("c", "Node")],
        )
        self.db.execute(
            "INSERT INTO tb_other(id,entity_type,parent__state,parent) VALUES ('x','Other','VALUE','a')"
        )
        self.db.executemany(
            'INSERT INTO tb_node_config(id,field,state,int,bool) VALUES (?,?,?,?,?)',
            [
                ('a', 'Node.limit', 'VALUE', '1', None),
                ('a', 'Node.active', 'VALUE', None, 1),
            ],
        )
        self.db.commit()

    def config(self, backend, path):
        return self.write(f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{path.as_posix()}'\n")

    def cli(self, backend, path):
        return main(
            self.api.SampleData, ['check-data', '-o', 'config', str(self.config(backend, path))]
        )

    def edge(self, identity, owner, target, **coords):
        s = self.layout.fields["Node." + identity]
        if s.table == "tb_node":
            self.db.execute(
                f'UPDATE tb_node SET "{s.state}" = ?, "{s.value}" = ? WHERE id = ?',
                ("VALUE", target, owner),
            )
        else:
            row = {"id": owner, **coords, s.state: "VALUE", s.value: target}
            self.db.execute(
                'INSERT INTO "'
                + s.table
                + '" ('
                + ",".join('"' + c + '"' for c in row)
                + ") VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
        self.db.commit()

    def report(self):
        reader = self.api.SampleReader.open(self.path).value
        try:
            return reader.validate()
        finally:
            reader.close()

    def test_deterministic_scc_witnesses_without_cycle_enumeration(self):
        self.assertEqual(cycles(set()), [])
        self.assertEqual(
            cycles({("a", "b"), ("b", "a"), ("b", "c"), ("c", "b"), ("d", "d"), ("x", "y")}),
            [("a", "b", "a"), ("d", "d")],
        )
        self.assertEqual(cycles({("a", "b"), ("b", "c"), ("c", "b")}), [("b", "c", "b")])

    def test_long_cycle_witness_is_iterative_and_deterministic(self):
        count = 10000
        nodes = [f'{i:05}' for i in range(count)]
        edges = set(zip(nodes, nodes[1:] + nodes[:1], strict=True))
        self.assertEqual(cycles(edges), [tuple(nodes + nodes[:1])])

    def test_field_and_root_scope_and_cross_inheritance_cycle(self):
        self.edge("parent", "a", "b")
        self.edge("other", "b", "a")
        self.edge("foreign", "a", "x")
        self.edge("parent", "c", "missing")
        self.assertTrue(self.report().value.valid)
        self.edge("parent", "b", "a")
        issue = self.report().value.issues[0]
        self.assertEqual(issue.field, "Node.parent")
        self.assertEqual(issue.ids, ("a", "b", "a"))
        self.edge("parent", "a", "a")
        self.assertEqual(self.report().value.issues[0].ids, ("a", "a"))

    def test_unresolvable_subtype_is_dangling_and_physical_owners_are_checked(self):
        self.edge('narrow', 'a', 'a')
        self.assertTrue(self.report().value.valid)
        self.edge('narrow', 'b', 'b')
        self.assertEqual(self.report().value.issues[0].ids, ('b', 'b'))
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute("INSERT INTO tb_node_children VALUES ('missing','a')")
        self.db.commit()
        self.assertIn('missing collection owner', str(self.report().value.issues))

    def test_explicit_full_check_includes_scd_and_config_group_invariants(self):
        self.edge('period', 'a', 'b', valid_from='2026-01-01', valid_to='2026-02-01')
        self.edge('period', 'a', 'b', valid_from='2026-01-15', valid_to='2026-03-01')
        self.db.execute("INSERT INTO tb_config_group (config_group_id) VALUES ('missing')")
        self.db.execute(
            "INSERT INTO tb_config_group (config_group_id, entity_type__state, entity_type, "
            "priority__state, priority, when__state, then_set__state) "
            "VALUES ('target', 'VALUE', 'Unknown', 'VALUE', '1', 'VALUE', 'VALUE')"
        )
        self.db.execute(
            "INSERT INTO tb_config_group (config_group_id, entity_type__state, entity_type, "
            "priority__state, priority, when__state, then_set__state) "
            "VALUES ('wrong-field', 'VALUE', 'Node', 'VALUE', '1', 'VALUE', 'VALUE')"
        )
        self.db.execute(
            "INSERT INTO tb_config_group_when VALUES ('wrong-field', 'Other.parent', ?) ",
            ('{"type":{},"value":"a"}',),
        )
        self.db.commit()
        expected = self.report().value
        self.assertFalse(expected.valid)
        messages = {issue.message for issue in expected.issues}
        self.assertIn('overlapping SCD intervals', messages)
        self.assertIn('ConfigGroup requires entity_type, priority, when and then_set', messages)
        self.assertIn('ConfigGroup target must be a business entity', messages)
        self.assertIn('ConfigGroup field is outside its target inheritance tree', messages)

        self.db.row_factory = sqlite3.Row
        rows = {
            table.name: [dict(row) for row in self.db.execute('SELECT * FROM "' + table.name + '"')]
            for table in self.layout.tables
        }
        files = self.root / 'files-invalid'
        for name, content in repository_contents(self.layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        reader = self.api.SampleReader.open_files(files).value
        self.addCleanup(reader.close)
        self.assertEqual(reader.validate().value, expected)
        managed = self.root / 'managed-invalid'
        with asyncio.run(self.api.SampleData.create(self.config('files', managed))) as store:
            store.publish_files(Data(self.layout, rows), None)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertNotEqual(self.cli('sqlite', self.path), 0)
            self.assertNotEqual(self.cli('files', managed), 0)
        self.assertIn('overlapping SCD intervals', output.getvalue())

    def test_explicit_full_check_validates_config_mapping_type_and_category(self):
        self.db.execute(
            "INSERT INTO tb_config_group (config_group_id, entity_type__state, entity_type, "
            "priority__state, priority, when__state, then_set__state) "
            "VALUES ('bad-map', 'VALUE', 'Node', 'VALUE', '1', 'VALUE', 'VALUE')"
        )
        type_ = self.layout.fields['Node.limit'].field.type
        value = json.dumps({'type': asdict(type_), 'value': '1'})
        self.db.execute(
            "INSERT INTO tb_config_group_when VALUES ('bad-map', 'Node.limit', ?)", (value,)
        )
        self.db.commit()
        report = self.report().value
        self.assertIn('ConfigGroup requires stored static conditions', str(report.issues))
        self.db.execute(
            "UPDATE tb_config_group_when SET value=? WHERE config_group_id='bad-map'",
            (json.dumps({'type': {}, 'value': '1'}),),
        )
        self.db.commit()
        report = self.report().value
        self.assertFalse(report.valid)
        self.assertIn('tb_config_group', str(report.issues))

    def test_explicit_file_check_rejects_corrupted_index(self):
        self.db.row_factory = sqlite3.Row
        rows = {
            table.name: [dict(row) for row in self.db.execute('SELECT * FROM "' + table.name + '"')]
            for table in self.layout.tables
        }
        files = self.root / 'files-bad-index'
        for name, content in repository_contents(self.layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        (files / 'indexes' / 'tb_node.jsonl').write_text('invalid\n', encoding='utf-8')
        reader = self.api.SampleReader.open_files(files).value
        self.addCleanup(reader.close)
        result = reader.validate()
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn('index digest mismatch', result.message)

    def test_cli_reports_read_failure_and_usage(self):
        self.db.execute('DROP TABLE tb_node_snap')
        self.db.commit()
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(output),
        ):
            self.assertNotEqual(self.cli('sqlite', self.path), 0)
        self.assertIn('tb_node_snap', output.getvalue())
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(output),
        ):
            self.assertNotEqual(self.cli('unknown', self.path), 0)
        self.assertIn('backend', output.getvalue())

    def test_dimensions_snapshot_dates_collections_and_scd_intersection(self):
        self.edge("dim", "a", "b", dim_source="X")
        self.edge("dim", "b", "a", dim_source="Y")
        self.edge("snap", "a", "b", date="2026-01-01")
        self.edge("snap", "b", "a", date="2026-01-02")
        self.edge("period", "a", "b", valid_from="2026-01-01", valid_to="2026-01-03")
        self.edge("period", "b", "a", valid_from="2026-01-03", valid_to="2026-01-05")
        self.assertTrue(self.report().value.valid)
        self.edge("dim", "b", "a", dim_source="X")
        self.edge("snap", "b", "a", date="2026-01-01")
        self.edge("period", "b", "a", valid_from="2026-01-02", valid_to="2026-01-03")
        self.db.execute("UPDATE tb_node SET children__state='VALUE' WHERE id IN ('a','b')")
        self.db.executemany("INSERT INTO tb_node_children VALUES (?, ?)", [("a", "b"), ("b", "a")])
        self.db.commit()
        issues = self.report().value.issues
        self.assertEqual(
            {i.field for i in issues},
            {"Node.dim", "Node.snap", "Node.period", "Node.children"},
        )
        self.assertEqual(next(i for i in issues if i.field == "Node.period").start, "2026-01-02")
        self.assertEqual(
            next(i for i in issues if i.field == "Node.dim").dimensions,
            (("source", "X"),),
        )
        self.assertEqual(next(i for i in issues if i.field == "Node.snap").start, "2026-01-01")

    def test_candidate_cycle_rejection_preserves_both_repositories(self):
        self.db.row_factory = sqlite3.Row
        rows = {
            t.name: [dict(r) for r in self.db.execute('SELECT * FROM "' + t.name + '"')]
            for t in self.layout.tables
        }
        files = self.root / 'managed-files'
        with asyncio.run(self.api.SampleData.create(self.config('files', files))) as data:
            data.publish_files(Data(self.layout, rows), None)
        plan = Plan(
            'cycle',
            self.layout.model.model_digest,
            (
                Group(
                    'invalid',
                    (
                        Operation('update', 'Node', ('key', 'limit'), (('a', 99),)),
                        Operation('update', 'Node', ('key', 'parent'), (('a', 'a'),)),
                    ),
                ),
            ),
        )
        source = self.write('cycle.yaplan', dumps(plan))
        for backend, path in (('sqlite', self.path), ('files', files)):
            with self.subTest(backend=backend):
                config = self.config(backend, path)
                with asyncio.run(self.api.SampleData.open(config)) as data:
                    before = data.snapshot().tables
                    with (
                        contextlib.redirect_stdout(io.StringIO()) as output,
                        contextlib.redirect_stderr(output),
                    ):
                        code = main(
                            self.api.SampleData,
                            ['apply', '-o', 'config', str(config), '-o', 'source', str(source)],
                        )
                    self.assertNotEqual(code, 0)
                    self.assertIn('candidate data validation failed', output.getvalue())
                    self.assertIn('same-field reference cycle', output.getvalue())
                    self.assertEqual(data.snapshot().tables, before)
                    self.assertEqual(list(data.transaction_directory().glob('*.yatran')), [])

    def test_two_backend_reports_cli_and_read_failures(self):
        self.edge("parent", "a", "a")
        self.db.row_factory = sqlite3.Row
        rows = {
            t.name: [dict(r) for r in self.db.execute('SELECT * FROM "' + t.name + '"')]
            for t in self.layout.tables
        }
        files = self.root / "files"
        for name, content in repository_contents(self.layout, rows).items():
            path = files / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        reader = self.api.SampleReader.open_files(files).value
        self.addCleanup(reader.close)
        self.assertEqual(reader.validate(), self.report())
        managed = self.root / 'managed-files'
        with asyncio.run(self.api.SampleData.create(self.config('files', managed))) as data:
            # Install a corrupt fixture without passing through candidate validation.
            data.publish_files(Data(self.layout, rows), None)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            for backend, path in [("sqlite", self.path), ("files", managed)]:
                self.assertNotEqual(self.cli(backend, path), 0)
            self.assertNotEqual(self.cli('sqlite', self.root / 'missing'), 0)
        self.assertIn("same-field reference cycle", output.getvalue())
        reader.close()
        self.assertEqual(reader.validate().status, Status.ERROR)
        self.db.execute("UPDATE tb_node SET parent__state='EMPTY', parent=NULL WHERE id='a'")
        self.db.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli('sqlite', self.path), 0)
            with patch(
                "yambl_compiler.storage.runtime.Reader.validate",
                side_effect=OSError("io"),
            ):
                self.assertNotEqual(self.cli('sqlite', self.path), 0)
        self.db.execute("PRAGMA ignore_check_constraints=ON")
        self.db.execute("UPDATE tb_node SET parent__state='VALUE',parent=NULL WHERE id='a'")
        self.db.commit()
        self.assertFalse(self.report().value.valid)
        self.db.execute("UPDATE tb_node SET entity_type='bad' WHERE id='a'")
        self.db.commit()
        self.assertIn("invalid owner", str(self.report().value.issues))
