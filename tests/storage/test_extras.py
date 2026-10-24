"""Check column-level relationships against resolved storage mappings."""

import re
import sqlite3
from contextlib import closing

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.extras import relations
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.rendering import ddl


class RelationsTests(FilesystemCase):
    def test_inherited_targets_shared_lanes_and_composite_foreign_keys(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'main.yambl',
            """entity Root {
                id key: string
                config attr choice: Child
                @sparse('links') attr sparse_link: Child
                @dimension(source: string)
                snapshot attr children: collection<Child>
            }
            entity Child inherits Root {}
            """,
        )
        layout = build_layout(compile_repository(self.root))
        diagram = relations(layout)
        self.assertEqual(diagram, relations(layout))
        tables = {table.name: table for table in layout.tables}
        nodes = {table.name: f't{i}' for i, table in enumerate(layout.tables)}
        root = nodes['tb_root']
        self.assertIn(
            f'{nodes["tb_root_config"]}::string ..> {root}::id : Root.choice [field=Root.choice]',
            diagram,
        )
        self.assertIn(
            f'{nodes["tb_root_links"]}::string ..> {root}::id : Root.sparse_link [field=Root.sparse_link]',
            diagram,
        )
        self.assertIn(
            f'{nodes["tb_root_children"]}::member_id ..> {root}::id : Root.children',
            diagram,
        )
        header = nodes['tb_root_children_status']
        self.assertIn(f'{header}::id ..> {root}::id : owner', diagram)
        self.assertNotIn('entity "tb_child"', diagram)
        node_tables = {node: tables[name] for name, node in nodes.items()}
        edges = re.findall(r'^(t\d+)::(\w+) (-->|\.\.>) (t\d+)::(\w+) : (.+)$', diagram, re.M)
        for source, column, _, target, target_column, _ in edges:
            self.assertIn(column, [c.name for c in node_tables[source].columns])
            self.assertIn(target_column, [c.name for c in node_tables[target].columns])
        physical = {(a, ac, b, bc) for a, ac, arrow, b, bc, _ in edges if arrow == '-->'}
        expected = set()
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript(ddl(layout))
            for table in layout.tables:
                for _, _, parent, child_col, parent_col, *_ in db.execute(
                    f'PRAGMA foreign_key_list("{table.name}")'
                ):
                    expected.add((nodes[table.name], child_col, nodes[parent], parent_col))
        self.assertEqual(physical, expected)
        keys = tables['tb_root_children_status'].primary
        self.assertEqual(len(keys), 3)
        for key in keys:
            self.assertIn(
                f'{nodes["tb_root_children"]}::{key} --> {header}::{key} : FK ({", ".join(keys)})',
                diagram,
            )
