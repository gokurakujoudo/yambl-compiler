"""Generate importable typed readers and publish complete output directories."""

import hashlib
import keyword
import re
import tempfile
from pathlib import Path

from yambl_compiler.ir.codec import dumps
from yambl_compiler.ir.model import Model, Type
from yambl_compiler.storage.backends import Row, repository_contents
from yambl_compiler.storage.contract import dumps as contract_json
from yambl_compiler.storage.documentation import (
    docstring,
    entity_documentation,
    field_documentation,
)
from yambl_compiler.storage.extras import python_reference, relations
from yambl_compiler.storage.layout import Layout, build_layout, snake
from yambl_compiler.storage.rendering import ddl, manifest, reference


def python_package(layout: Layout, package: str) -> str:
    """Render explicit typed methods delegating to the shared runtime.

    :param layout: Checked storage mappings.
    :param package: Python package name.
    :returns: Importable Python source.
    :raises ValueError: On generated public-name collisions.
    """
    names = {key: entity.name.rsplit(".", 1)[-1] for key, entity in layout.entities.items()}
    codes: dict[str, str] = {}

    def python_type(type_: Type) -> str:
        """Render a supported Python value annotation.

        :param type_: Resolved supported IR type.
        :returns: Python annotation.
        """
        if type_.kind == "reference":
            return f"EntityRef[{names[type_.target]}]"
        if type_.name == "collection":
            return f"frozenset[{python_type(type_.arguments[0])}]"
        if type_.name == "code":
            target = type_.arguments[0].target
            codes[target] = target.rsplit(".", 1)[-1] + "Code"
            if not codes[target].isidentifier():
                raise ValueError(f"{target}: unsupported Python code type name")
            return codes[target]
        return {
            "string": "str",
            "bool": "bool",
            "integer": "int",
            "snowflake_id": "int",
            "decimal": "Decimal",
            "date": "date",
        }[type_.name]

    for stored in layout.fields.values():
        if stored.root not in layout.entities:
            continue
        python_type(stored.field.type)
        for dim in stored.dimensions:
            python_type(dim.type)
    reader_name = (
        "".join(word.capitalize() for word in package.removesuffix("_model").split("_")) + "Reader"
    )
    records = {
        key: names[s.root] + "".join(p.capitalize() for p in s.field.name.split("_")) + "Record"
        for key, s in layout.fields.items()
        if s.root in names and (s.dimensions or s.field.category in ("snapshot", "scd"))
    }
    public_names = [*names.values(), *codes.values(), *records.values(), reader_name]
    reader_methods = [
        "get_" + snake(name) + suffix for name in names.values() for suffix in ("", "_many")
    ]
    if len(set(reader_methods)) != len(reader_methods) or "get_many" in reader_methods:
        raise ValueError("generated reader method collision")
    reserved = {
        "EntityHandle",
        "Reader",
        "ReadResult",
        "EntityRef",
        "Success",
        "NonNormalResult",
        "Status",
        "Path",
        "Decimal",
        "StrEnum",
        "date",
        "cast",
        "dataclass",
        "ValidationReport",
        "ValidationIssue",
    }
    if len(set(public_names)) != len(public_names) or set(public_names) & reserved:
        raise ValueError("generated Python public-name collision")
    lines = [
        '"""Generated typed read-only model API."""',
        "",
        "from __future__ import annotations",
        "",
        "from datetime import date",
        "from dataclasses import dataclass",
        "from decimal import Decimal",
        "from enum import StrEnum",
        "from pathlib import Path",
        "from typing import cast",
        "",
        "from ._results import EntityRef, NonNormalResult, ReadResult, Status, Success",
        "from ._runtime import EntityHandle, Reader",
        "from ._validation import ValidationReport, ValidationIssue",
        "",
    ]
    for target, name in sorted(codes.items()):
        dictionary = next((d for d in layout.model.definitions if d.identity == target), None)
        lines.extend(
            [
                "",
                f'class {name}({"StrEnum" if dictionary else "str"}):',
                f'    """Codes belonging to {target}."""',
            ]
        )
        if dictionary:
            for entry in dictionary.entries:
                if (
                    not entry.code.isidentifier()
                    or keyword.iskeyword(entry.code)
                    or entry.code in ("name", "value")
                ):
                    raise ValueError(f"{target}.{entry.code}: unsupported Python enum member")
                lines.append(f"    {entry.code} = {entry.code!r}")
    for identity, name in records.items():
        stored = layout.fields[identity]
        f = stored.field
        coordinates = [(d.key, python_type(d.type)) for d in stored.dimensions]
        if f.category == "snapshot":
            time_key = next(
                (b.key for c in f.decorators if c.target == "builtins.temporal" for b in c.bound),
                "as_of_date",
            )
            coordinates.append((time_key, "date"))
        elif f.category == "scd":
            coordinates.extend([("valid_from", "date"), ("valid_to", "date")])
        keys = [k for k, _ in coordinates]
        if len(set(keys)) != len(keys) or any(k == "value" or keyword.iskeyword(k) for k in keys):
            raise ValueError(
                f"{identity}: generated time parameter collision or record member collision"
            )
        lines.extend(["", "", "@dataclass(frozen=True)", f"class {name}:"])
        lines.extend(
            docstring(
                [
                    f"One stored coordinate of {identity}.",
                    "",
                    *(f":param {k}: Stored coordinate ({t})." for k, t in coordinates),
                    ":param value: Decoded payload or per-record non-normal result.",
                ],
                4,
            )
        )
        lines.extend(f"    {k}: {t}" for k, t in coordinates)
        lines.append(f"    value: ReadResult[{python_type(f.type)}]")
    ordered = sorted(layout.entities, key=lambda k: len(_ancestors(layout, k)))
    for key in ordered:
        entity = layout.entities[key]
        parent = names.get(entity.parent, "EntityHandle")
        lines.extend(
            [
                "",
                "",
                "@dataclass(frozen=True, eq=False)",
                f"class {names[key]}({parent}):",
                *entity_documentation(entity),
            ]
        )
        if layout.roots[key] != key:
            continue
        methods: set[str] = set()
        root_fields = [s.field for s in layout.fields.values() if s.root == key]
        method_names = {"read_" + f.name + suffix for f in root_fields for suffix in ("", "_range")}
        for f in root_fields:
            if f.name in {"reader", "entity_type", "id", "_cache", "_full"} | method_names:
                raise ValueError(f"{f.identity}: generated property collision")
        for stored in layout.fields.values():
            if stored.root != key:
                continue
            f = stored.field
            output = python_type(f.type)
            dims = ", ".join(f"{d.key}: {python_type(d.type)}" for d in stored.dimensions)
            dimensions = "{" + ", ".join(f"{d.key!r}: {d.key}" for d in stored.dimensions) + "}"
            kw = ", *, " + dims if dims else ""
            method = "read_" + f.name
            if method in methods:
                raise ValueError(f"{key}: generated method collision {method}")
            methods.add(method)
            temporal = f.category in ("snapshot", "scd")
            time_key = next(
                (b.key for c in f.decorators if c.target == "builtins.temporal" for b in c.bound),
                "as_of_date",
            )
            if temporal and (
                keyword.iskeyword(time_key)
                or time_key in {"self", *(d.key for d in stored.dimensions)}
                or any(d.key in ("start_date", "end_date") for d in stored.dimensions)
            ):
                raise ValueError(f"{f.identity}: generated time parameter collision")
            point_arg = f", {time_key}: date" if temporal else ""
            when = f", {time_key}" if temporal else ""
            property_type = (
                f"tuple[{records[f.identity]}, ...]" if f.identity in records else output
            )
            lines.extend(
                [
                    "",
                    "    @property",
                    f"    def {f.name}(self) -> ReadResult[{property_type}]:",
                    *field_documentation(layout, stored, property_type, "", property_=True),
                    f"        return cast(ReadResult[{property_type}], self.reader.read_all(self, {f.identity!r}))",
                ]
            )
            lines.extend(
                [
                    "",
                    f"    def {method}(self{point_arg}{kw}) -> ReadResult[{output}]:",
                    *field_documentation(layout, stored, output, time_key if temporal else ""),
                    f"        return cast(ReadResult[{output}], self.reader.read(self, {f.identity!r}, {dimensions}{when}))",
                ]
            )
            if temporal:
                range_method = method + "_range"
                if range_method in methods or any(
                    "read_" + s.field.name == range_method
                    for s in layout.fields.values()
                    if s.root == key
                ):
                    raise ValueError(f"{key}: generated range method collision")
                methods.add(range_method)
                result = f"ReadResult[list[tuple[date, ReadResult[{output}]]]]"
                lines.extend(
                    [
                        "",
                        f"    def {range_method}(self, start_date: date, end_date: date{kw}) -> {result}:",
                        *field_documentation(layout, stored, output, time_key, True),
                        f"        return cast({result}, self.reader.read_range(self, {f.identity!r}, {dimensions}, start_date, end_date))",
                    ]
                )
    lines.extend(
        [
            "",
            "",
            f"class {reader_name}(Reader):",
            '    """Open SQLite or JSONL data with immutable lazy entity handles."""',
            "    model_path = Path(__file__).with_name('runtime-contract.json')",
            f'    contract_digest = {hashlib.sha256(contract_json(layout).encode("utf-8")).hexdigest()!r}',
            "    record_types = {" + ", ".join(f"{k!r}: {v}" for k, v in records.items()) + "}",
            "    entity_types = {" + ", ".join(f"{k!r}: {v}" for k, v in names.items()) + "}",
            "    code_types = {" + ", ".join(f"{k!r}: {v}" for k, v in codes.items()) + "}",
        ]
    )
    for key, name in names.items():
        id_type = python_type(layout.ids[layout.roots[key]].type)
        method = "get_" + snake(name)
        lines.extend(
            [
                "",
                f"    def {method}(self, id_value: {id_type}) -> ReadResult[{name}]:",
                *docstring(
                    [
                        f"Read one {key} or subtype identity.",
                        "",
                        f":param id_value: ID of the requested {key} instance.",
                        f":type id_value: {id_type}",
                        ":returns: Success containing the actual subtype handle, NO_ENTRY when absent or outside the requested subtype, or ERROR on failure.",
                        f":rtype: ReadResult[{name}]",
                    ],
                    8,
                ),
                f"        return cast(ReadResult[{name}], self.get({key!r}, id_value))",
                "",
                f"    def {method}_many(self, ids: list[{id_type}]) -> ReadResult[list[ReadResult[{name}]]]:",
                *docstring(
                    [
                        f"Read {key} IDs in order, preserving duplicates.",
                        "",
                        ":param ids: IDs in requested result order; repeated IDs retain separate positions.",
                        f":type ids: list[{id_type}]",
                        ":returns: Success containing one handle or NO_ENTRY result per ID. Empty input succeeds with an empty list; any query failure returns outer ERROR without a partial list.",
                        f":rtype: ReadResult[list[ReadResult[{name}]]]",
                    ],
                    8,
                ),
                f"        return cast(ReadResult[list[ReadResult[{name}]]], self.get_many({key!r}, list(ids)))",
            ]
        )
    lines.extend(
        [
            "",
            "__all__ = "
            + repr(
                sorted(
                    [
                        *public_names,
                        "EntityRef",
                        "ReadResult",
                        "Success",
                        "NonNormalResult",
                        "Status",
                        "ValidationReport",
                        "ValidationIssue",
                    ]
                )
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _ancestors(layout: Layout, key: str) -> list[str]:
    """Order generated classes before their subclasses.

    :param layout: Checked inheritance graph.
    :param key: Entity identity.
    :returns: Business ancestors.
    """
    result: list[str] = []
    while layout.entities[key].parent in layout.entities:
        key = layout.entities[key].parent
        result.append(key)
    return result


def generate(model: Model, output: Path, package: str, dry_run: bool = False) -> Layout:
    """Publish a complete new generated directory after preflight.

    :param model: Validated IR model.
    :param output: New or empty destination directory.
    :param package: Explicit snake_case Python package name.
    :param dry_run: Validate and render without filesystem writes.
    :returns: Physical model used for every output.
    :raises ValueError: If generation is unsupported or output is nonempty.
    :raises OSError: If publication fails; partial files stay outside the destination.
    """
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", package) or keyword.iskeyword(package):
        raise ValueError("package must be a non-keyword snake_case identifier")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("generate requires a new or empty output directory")
    layout = build_layout(model)
    source = python_package(layout, package)
    contents = {
        "schema.sql": ddl(layout),
        "schema.md": reference(layout)
        + "\n## Table relationships\n\n```mermaid\n"
        + relations(layout)
        + "```\n",
        "schema-relations.mmd": relations(layout),
        "python-schema.md": python_reference(source, package),
        "storage-manifest.json": manifest(layout),
        package + "/__init__.py": source,
        package + "/runtime-contract.json": contract_json(layout),
        package + "/py.typed": "",
        package + "/model.yambl-ir.json": dumps(model),
    }
    modules = [
        "records",
        "contract",
        "backends",
        "results",
        "runtime",
        "validation",
        "cli_runtime",
    ]
    for name in [*modules, "ir_model"]:
        path = (
            Path(__file__).with_name(name + ".py")
            if name != "ir_model"
            else Path(__file__).parents[1] / "ir/model.py"
        )
        text = path.read_text(encoding="utf-8").replace("yambl_compiler.ir.model", "._ir_model")
        for module in modules:
            text = text.replace("yambl_compiler.storage." + module, "._" + module)
        contents[package + "/_" + name + ".py"] = text
    reader_name = (
        "".join(word.capitalize() for word in package.removesuffix("_model").split("_")) + "Reader"
    )
    contents[package + "/__main__.py"] = (
        f'"""Validate model data from the command line."""\nfrom . import {reader_name}\nfrom ._cli_runtime import main\n\nraise SystemExit(main({reader_name}))\n'
    )
    tables: dict[str, list[Row]] = {
        t.name: [dict(zip((c.name for c in t.columns), row, strict=True)) for row in t.rows]
        for t in layout.tables
    }
    tables["tb_yambl_metadata"] = [
        {"key": "layout_revision", "value": "4"},
        {"key": "model_digest", "value": model.model_digest},
    ]
    contents.update(
        {"repository/" + name: text for name, text in repository_contents(layout, tables).items()}
    )
    if not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".yambl-generate-") as temporary:
            stage = Path(temporary) / "output"
            stage.mkdir()
            for name, text in contents.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8", newline="\n")
            if output.exists():
                output.rmdir()
            stage.rename(output)
    return layout
