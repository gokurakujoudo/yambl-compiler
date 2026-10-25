"""Generate importable typed readers and publish complete output directories."""

import hashlib
import keyword
import re
import tempfile
from pathlib import Path

from yambl_compiler.ir.codec import dumps
from yambl_compiler.ir.model import CalculationInput, Field, Model, Type
from yambl_compiler.storage.backends import Row, repository_contents
from yambl_compiler.storage.contract import dumps as contract_json
from yambl_compiler.storage.documentation import (
    docstring,
    entity_documentation,
    field_documentation,
    literal_section,
    metadata,
)
from yambl_compiler.storage.extras import python_reference, relations
from yambl_compiler.storage.layout import Layout, build_layout, snake
from yambl_compiler.storage.programs import programs
from yambl_compiler.storage.records import LAYOUT_REVISION
from yambl_compiler.storage.rendering import ddl, manifest, reference
from yambl_compiler.storage.value_types import generate_values


def python_package(layout: Layout, package: str) -> str:
    """Render explicit typed methods delegating to the shared runtime.

    :param layout: Checked storage mappings.
    :param package: Python package name.
    :returns: Importable Python source.
    :raises ValueError: On generated public-name collisions.
    """
    names = {key: entity.name.rsplit(".", 1)[-1] for key, entity in layout.entities.items()}
    codes: dict[str, str] = {}
    value_names = {
        d.identity: d.name.rsplit('.', 1)[-1] for d in layout.model.definitions if d.kind == 'value'
    }

    def python_type(type_: Type) -> str:
        """Render a supported Python value annotation.

        :param type_: Resolved supported IR type.
        :returns: Python annotation.
        """
        if type_.target in value_names and type_.kind == 'value':
            return value_names[type_.target]
        if type_.kind == "reference":
            return f"EntityRef[{names.get(type_.target, 'object')}]"
        if type_.name == "mapping":
            return f"Mapping[{python_type(type_.arguments[0])}, {python_type(type_.arguments[1])}]"
        if type_.name in ("collection", 'values'):
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
            "float": "float",
            "date": "date",
            'datetime': 'datetime',
        }[type_.name]

    for definition in layout.model.definitions:
        if definition.kind == 'value':
            for field in definition.fields:
                python_type(field.type)
    for stored in layout.fields.values():
        if stored.root not in layout.entities:
            continue
        python_type(stored.field.type)
        for dim in stored.dimensions:
            python_type(dim.type)
    computed: dict[str, Field] = {}
    roots: dict[str, str] = {}
    for entity in layout.entities.values():
        for field in entity.fields:
            if field.calculation is not None:
                computed.setdefault(field.identity, field)
                roots[field.identity] = layout.roots[entity.identity]
                python_type(field.type)
                for parameter in field.calculation.inputs:
                    python_type(parameter.type)
    requests = {
        key: names[f.implementation.rsplit(".", 1)[0]]
        + "".join(p.capitalize() for p in f.name.split("_"))
        + "Request"
        for key, f in computed.items()
    }

    def parameter_type(parameter: CalculationInput) -> str:
        """Represent omission explicitly in generated public signatures.

        :param parameter: Inferred input contract.
        :returns: Strong Python annotation, including Omitted for optional inputs.
        """
        annotation = python_type(parameter.type)
        if parameter.type.kind == "reference":
            annotation += " | str"
        if not parameter.required or parameter.default is not None:
            annotation += " | Omitted"
        return annotation

    def parameters(field: Field) -> list[str]:
        """Render keyword-only inferred arguments.

        :param field: Checked computed declaration.
        :returns: Annotated arguments with omission defaults.
        """
        assert field.calculation is not None
        return [
            p.name
            + ": "
            + parameter_type(p)
            + (" = UNSET" if not p.required or p.default is not None else "")
            for p in field.calculation.inputs
        ]

    def computed_doc(field: Field) -> list[str]:
        """Describe inferred parameters, provenance and computation caching.

        :param field: Checked computed declaration.
        :returns: Method documentation prose.
        """
        assert field.calculation is not None
        owners = sorted(
            {
                f.implementation.rsplit(".", 1)[0]
                for d in layout.model.definitions
                for f in d.fields
                if f.identity == field.identity
            }
        )
        text = [
            f"Calculate {field.identity}.",
            "",
            *metadata(field.decorators),
            *literal_section("Declaring entities", "\n".join(owners)),
            "Shared dependencies are read in batches. Queries cache by instance, normalized parameters and best_effort; ERROR is not cached.",
            "",
            ":param best_effort: Keep successful contributions with PARTIAL when non-error rows fail; defaults to false.",
        ]
        for p in field.calculation.inputs:
            descriptions = [
                item.type.exact.text
                for call in field.decorators
                if call.target == 'builtins.extra_input_params'
                and any(b.name == 'parameter' and b.key == p.name for b in call.bound)
                for item in call.bound
                if item.name == 'description' and item.type.exact is not None
            ]
            for description in descriptions:
                text.append('')
                text.extend(literal_section('Input ' + p.name, description))
            sources = ", ".join(sorted({f"{s.path}:{s.line}:{s.column}" for s in p.sites}))
            default = (
                (" Defaults to input." + p.default.value + " when omitted.")
                if p.default and p.default.kind == "member"
                else (" Uses the declared default when omitted." if p.default else "")
            )
            text.extend(
                [
                    f":param {p.name}: Inferred dependency parameter.{default} Sources: {sources}.",
                    f":type {p.name}: {parameter_type(p)}",
                ]
            )
        text.extend(
            [
                ":returns: Immutable computed payload, PARTIAL with issues, or a native non-normal result.",
                f":rtype: ReadResult[{python_type(field.type)}]",
            ]
        )
        return text

    reader_name = (
        "".join(word.capitalize() for word in package.removesuffix("_model").split("_")) + "Reader"
    )
    records = {
        key: names[s.root] + "".join(p.capitalize() for p in s.field.name.split("_")) + "Record"
        for key, s in layout.fields.items()
        if s.root in names and (s.dimensions or s.field.category in ("snapshot", "scd"))
    }
    public_names = [
        *names.values(),
        *codes.values(),
        *records.values(),
        *requests.values(),
        *value_names.values(),
        reader_name,
    ]
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
        "Partial",
        "CalculationIssue",
        "Omitted",
        "UNSET",
        "PROGRAMS",
        "Mapping",
        "Sequence",
    }
    if len(set(public_names)) != len(public_names) or set(public_names) & reserved:
        raise ValueError("generated Python public-name collision")
    lines = [
        '"""Generated typed read-only model API."""',
        "",
        "from __future__ import annotations",
        "",
        "from datetime import date, datetime",
        "from collections.abc import Mapping, Sequence",
        "from dataclasses import dataclass",
        "from decimal import Decimal",
        "from enum import StrEnum",
        "from pathlib import Path",
        "from typing import cast",
        "",
        "from ._results import CalculationIssue, EntityRef, NonNormalResult, Partial, ReadResult, Status, Success",
        "from ._computing import Omitted, UNSET",
        "from ._calculations import PROGRAMS",
        "from ._runtime import EntityHandle, Reader",
        "from ._validation import ValidationReport, ValidationIssue",
        "",
    ]
    lines.append('from ._value_types import ' + ', '.join(sorted(value_names.values())))
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
    for identity, name in requests.items():
        field = computed[identity]
        assert field.calculation is not None
        reserved_parameters = {"self", "id_value", "best_effort"}
        if any(
            keyword.iskeyword(p.name) or p.name in reserved_parameters
            for p in field.calculation.inputs
        ):
            raise ValueError(f"{identity}: generated calculation parameter collision")
        lines.extend(
            [
                "",
                "",
                "@dataclass(frozen=True, kw_only=True)",
                f"class {name}:",
                *docstring(
                    [
                        f"One request for {identity}.",
                        "",
                        ":param id_value: Requested entity ID.",
                        "",
                        *computed_doc(field)[2:],
                    ],
                    4,
                ),
                f"    id_value: {python_type(layout.ids[roots[identity]].type)}",
                *(f"    {p}" for p in parameters(field)),
                "    best_effort: bool = False",
            ]
        )
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
        for identity, field in computed.items():
            if roots[identity] != key:
                continue
            assert field.calculation is not None
            method = "read_" + field.name
            if method in method_names or {method, field.name} & {f.name for f in root_fields}:
                raise ValueError(f"{identity}: generated calculation method collision")
            methods.add(method)
            args = ", ".join([*parameters(field), "best_effort: bool = False"])
            values = (
                "{" + ", ".join(f"{p.name!r}: {p.name}" for p in field.calculation.inputs) + "}"
            )
            output = python_type(field.type)
            lines.extend(
                [
                    "",
                    f"    def {method}(self, *, {args}) -> ReadResult[{output}]:",
                    *docstring(computed_doc(field), 8),
                    f"        return cast(ReadResult[{output}], self.reader.calculate(self, {identity!r}, {values}, best_effort))",
                ]
            )
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
            "    calculation_programs = PROGRAMS",
        ]
    )
    for identity, name in requests.items():
        field = computed[identity]
        assert field.calculation is not None
        owner = field.implementation.rsplit(".", 1)[0]
        method = "read_" + snake(names[owner]) + "_" + field.name + "_many"
        result = f"ReadResult[tuple[ReadResult[{python_type(field.type)}], ...]]"
        values = (
            "{" + ", ".join(f"{p.name!r}: request.{p.name}" for p in field.calculation.inputs) + "}"
        )
        lines.extend(
            [
                "",
                f"    def {method}(self, requests: Sequence[{name}]) -> {result}:",
                *docstring(
                    [
                        f"Calculate {identity} for an ordered batch.",
                        "",
                        ":param requests: Independent request parameters; order and repeated requests are preserved.",
                        ":returns: Per-request results; whole-backend failures return outer ERROR.",
                        f":rtype: {result}",
                    ],
                    8,
                ),
                f"        batch: tuple[tuple[str | int, dict[str, object], bool], ...] = tuple((request.id_value, {values}, request.best_effort) for request in requests)",
                f"        return cast({result}, self.calculate_many({owner!r}, {identity!r}, batch))",
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
                        "Partial",
                        "CalculationIssue",
                        "Omitted",
                        "UNSET",
                        "ValidationReport",
                        "ValidationIssue",
                    ]
                )
            ),
            "",
        ]
    )
    if not any(re.search(r'\bdatetime\b', line) for line in lines if not line.startswith('from ')):
        lines[lines.index('from datetime import date, datetime')] = 'from datetime import date'
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
        + "\n## Table relationships\n\n[PlantUML source](schema-relations.puml)\n\n```plantuml\n"
        + relations(layout)
        + "```\n",
        "schema-relations.puml": relations(layout),
        "python-schema.md": python_reference(source, package),
        "storage-manifest.json": manifest(layout),
        package + "/__init__.py": source,
        package + "/runtime-contract.json": contract_json(layout),
        package + "/py.typed": "",
        package + "/model.yambl-ir.json": dumps(model),
        package + "/_calculations.py": programs(model),
        package + '/_value_types.py': generate_values(model),
    }
    modules = [
        "records",
        "contract",
        "backends",
        "batch",
        "configuration",
        "arithmetic",
        "conversions",
        "selection",
        "computing",
        "session",
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
        text = text.replace("yambl_compiler.computation.records", "._computation_records")
        for module in modules:
            text = text.replace("yambl_compiler.storage." + module, "._" + module)
        contents[package + "/_" + name + ".py"] = text
    contents[package + "/_computation_records.py"] = (
        Path(__file__).parents[1] / "computation/records.py"
    ).read_text(encoding="utf-8")
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
        {"key": "layout_revision", "value": str(LAYOUT_REVISION)},
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
