"""Render relationship diagrams and the generated Python API reference."""

import ast

from yambl_compiler.storage.records import Layout, table_name


def relations(layout: Layout) -> str:
    """Describe physical and logical links from the shared layout.

    :param layout: Checked table and field mappings.
    :returns: Mermaid flowchart including every table.
    """
    nodes = {t.name: "t" + str(i) for i, t in enumerate(layout.tables)}
    lines = ["flowchart LR"]
    lines.extend(f'    {node}["{name}"]' for name, node in nodes.items())
    links: set[tuple[str, str, str]] = set()
    for table in layout.tables:
        links.update((table.name, parent, "-->") for parent in table.parents)
    for stored in layout.fields.values():
        type_ = stored.field.type
        if type_.name == "collection":
            type_ = type_.arguments[0]
        if type_.kind == "reference":
            target = table_name(layout.roots.get(type_.target, type_.target))
            links.add((stored.members or stored.table, target, "-.->"))
    lines.extend(f"    {nodes[a]} {arrow} {nodes[b]}" for a, b, arrow in sorted(links))
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
        "Requires Python 3.14 or later and only the standard library.",
        "",
        "Reader.open_sqlite(path) and Reader.open_files(path) open existing data. Reader.open(path) is the SQLite alias. All return ReadResult[Reader].",
        "",
        "Entities and coordinate records are frozen dataclasses. Entity equality retains object identity. References are resolved explicitly with reader.resolve(ref).",
        "",
        "Properties fetch all coordinates once. Methods cache normalized query arguments and reuse complete property data. ERROR is never cached. Cached data survives close; new backend reads return ERROR. Range methods return independent lists.",
        "",
        "reader.validate() returns ReadResult[ValidationReport]. Run python -m "
        + package
        + " check-data --backend sqlite|files --path PATH for explicit whole-repository validation.",
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
