"""Render relationship diagrams and the generated Python API reference."""

import ast

from yambl_compiler.storage.records import Layout, table_name


def relations(layout: Layout) -> str:
    """Describe physical and logical links from the shared layout.

    :param layout: Checked table and field mappings.
    :returns: PlantUML entities with columns and column-level relationships.
    """
    nodes = {t.name: "t" + str(i) for i, t in enumerate(layout.tables)}
    tables = {table.name: table for table in layout.tables}
    lines = [
        "@startuml",
        "left to right direction",
        "hide circle",
        "skinparam backgroundColor white",
        "skinparam classAttributeIconSize 0",
    ]
    links: set[tuple[str, str, str, str, str, str]] = set()
    for table in layout.tables:
        lines.append(f'entity "{table.name}" as {nodes[table.name]} {{')
        for column in table.columns:
            primary = (
                f" <<PK {table.primary.index(column.name) + 1}>>"
                if column.name in table.primary
                else ""
            )
            nullable = "NULL" if column.nullable else "NOT NULL"
            lines.append(f"    {column.name} : {column.sql_type} {nullable}{primary}")
        lines.append("}")
        for parent in table.parents:
            # Layout foreign keys share the parent's ordered primary-key columns.
            keys = tables[parent].primary
            for key in keys:
                links.add((table.name, key, parent, key, "-->", "FK (" + ", ".join(keys) + ")"))
    for stored in layout.fields.values():
        owner = table_name(stored.root)
        if stored.table != owner:
            links.add((stored.table, "id", owner, "id", "..>", "owner"))
        type_ = stored.field.type
        if type_.name == "collection":
            type_ = type_.arguments[0]
        if type_.kind == "reference":
            target = table_name(layout.roots.get(type_.target, type_.target))
            source = stored.members or stored.table
            column_name = "member_id" if stored.members else stored.value
            label = stored.field.identity
            if stored.key_values:
                label += " [" + ", ".join(f"{k}={v}" for k, v in stored.key_values) + "]"
            links.add((source, column_name, target, tables[target].primary[0], "..>", label))
    lines.extend(
        f"{nodes[a]}::{ac} {arrow} {nodes[b]}::{bc} : {label}"
        for a, ac, b, bc, arrow, label in sorted(links)
    )
    lines.extend(
        [
            "legend bottom",
            "PK n: position in the composite primary key",
            "Solid arrow: enforced foreign key; repeated FK labels form one composite constraint",
            "Dashed arrow: logical owner or entity reference (not enforced by SQLite)",
            "endlegend",
            "@enduml",
        ]
    )
    return "\n".join(lines) + "\n"


def python_reference(source: str, package: str) -> str:
    """Document generated declarations without importing the output package.

    :param source: Generated Python class definitions and rST docstrings.
    :param package: Importable package name.
    :returns: Standalone Markdown API reference.
    """
    lines = [
        f"# {package} Python API",
        "",
        "Requires Python 3.14 or later and the runtime dependencies declared in the generated pyproject.toml.",
        "",
        "Use await Data.open(config_path) or await Data.create(config_path) on the generated model-specific Data class. The .lclcfg file selects the backend and repository path. data.read() returns a reader with a stable snapshot.",
        "",
        "Entities and coordinate records are frozen dataclasses. Entity equality retains object identity. References are resolved explicitly with reader.resolve(ref).",
        "",
        "Properties fetch all coordinates once. Methods cache normalized query arguments and reuse complete property data. ERROR is never cached. Cached data survives close; new backend reads return ERROR. Range methods return independent lists.",
        "",
        "reader.validate() returns ReadResult[ValidationReport]. Run python -m "
        + package
        + " check-data -o config repository.lclcfg for explicit whole-repository validation.",
        "",
    ]
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        lines.extend(["## " + node.name, "", "```rst", ast.get_docstring(node) or "", "```", ""])
        for member in node.body:
            if isinstance(member, ast.AnnAssign):
                lines.extend(["```python", ast.unparse(member), "```", ""])
            elif isinstance(member, ast.FunctionDef):
                lines.extend(
                    [
                        "### " + node.name + "." + member.name,
                        "",
                        "```python",
                        f'def {member.name}({ast.unparse(member.args)}) -> {ast.unparse(member.returns) if member.returns else "None"}',
                        "```",
                        "",
                        "```rst",
                        ast.get_docstring(member) or "",
                        "```",
                        "",
                    ]
                )
    return "\n".join(lines).rstrip() + "\n"
