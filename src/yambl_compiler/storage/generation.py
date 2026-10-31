"""Generate importable typed readers and publish complete output directories."""

import hashlib
import json
import keyword
import re
import tempfile
from importlib.metadata import requires
from pathlib import Path

from yambl_compiler.ir.codec import dumps
from yambl_compiler.ir.model import CalculationInput, Call, Field, Model, Type
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
from yambl_compiler.storage.value_types import code_type_name, generate_values


def python_package(layout: Layout, package: str) -> str:
    """Render explicit typed methods delegating to the shared runtime.

    :param layout: Checked storage mappings.
    :param package: Python package name.
    :returns: Importable Python source.
    :raises ValueError: On unsupported types or generated public-name collisions.
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
            annotation = f"EntityRef[{names.get(type_.target, 'object')}]"
            return annotation + (' | HierarchyView' if type_.target == 'builtins.Hierarchy' else '')
        if type_.name == "mapping":
            return f"Mapping[{python_type(type_.arguments[0])}, {python_type(type_.arguments[1])}]"
        if type_.name in ("collection", 'values'):
            return f"frozenset[{python_type(type_.arguments[0])}]"
        if type_.name == "code":
            target = type_.arguments[0].target
            name = code_type_name(target)
            for other, existing in codes.items():
                if other != target and existing == name:
                    raise ValueError(
                        f"generated Python code type name collision: {name} ({other}, {target})"
                    )
            codes[target] = name
            return codes[target]
        return {
            "string": "str",
            "type": "str",
            'attrs': 'str',
            'dependent': 'object',
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
        if layout.entities[stored.root].source.origin == "builtin":
            continue
        python_type(stored.field.type)
        for dim in stored.dimensions:
            python_type(dim.type)
    computed: dict[str, Field] = {}
    roots: dict[str, str] = {}
    abstract_owners: dict[str, str] = {}
    contracts = {field.identity: field.inputs for field in layout.model.fields}
    for entity in layout.entities.values():
        for field in entity.fields:
            if field.abstract:
                owner = field.implementation.rsplit('.', 1)[0]
                current = abstract_owners.get(field.identity)
                if current is None or len(_ancestors(layout, owner)) < len(
                    _ancestors(layout, current)
                ):
                    abstract_owners[field.identity] = owner
            if field.calculation is not None:
                computed.setdefault(field.identity, field)
                roots[field.identity] = layout.roots[entity.identity]
                python_type(field.type)
                for parameter in contracts[field.identity]:
                    python_type(parameter.type)
    lookups = {
        (key, rule.name): rule for key, entity in layout.entities.items() for rule in entity.lookups
    }
    lookup_names = {
        identity: names[identity[0]] + ''.join(part.capitalize() for part in identity[1].split('_'))
        for identity in lookups
    }
    for rule in lookups.values():
        for key_item in rule.keys:
            python_type(key_item.type)
        for input_item in rule.inputs:
            python_type(input_item.type)
        for key_item in rule.keys:
            if not key_item.name.isidentifier() or keyword.iskeyword(key_item.name):
                raise ValueError(
                    f'{key_item.source.path}:{key_item.source.line}:{key_item.source.column}: invalid Python lookup key name: {key_item.name}'
                )
        for input_item in rule.inputs:
            if (
                not input_item.name.isidentifier()
                or keyword.iskeyword(input_item.name)
                or input_item.name == 'self'
            ):
                raise ValueError(
                    f'{rule.source.path}:{rule.source.line}:{rule.source.column}: invalid Python lookup parameter name: {input_item.name}'
                )
    requests = {
        key: names[abstract_owners.get(key, f.implementation.rsplit(".", 1)[0])]
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
        return [
            p.name
            + ": "
            + parameter_type(p)
            + (" = UNSET" if not p.required or p.default is not None else "")
            for p in contracts[field.identity]
        ]

    def input_descriptions(calls: tuple[Call, ...], name: str) -> list[str]:
        """Render the descriptions declared for one input as safe literal blocks.

        :param calls: Metadata applied to the declaration.
        :param name: Input parameter name.
        :returns: rST literal sections in declaration order.
        """
        return [
            line
            for call in calls
            if call.target == 'builtins.extra_input_param'
            and any(bound.name == 'parameter' and bound.key == name for bound in call.bound)
            for bound in call.bound
            if bound.name == 'description' and bound.type.exact is not None
            for line in literal_section('Input ' + name, bound.type.exact.text)
        ]

    def computed_doc(field: Field) -> list[str]:
        """Describe inferred parameters, provenance and computation caching.

        :param field: Checked computed declaration.
        :returns: Method documentation prose.
        """
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
        for p in contracts[field.identity]:
            descriptions = input_descriptions(field.decorators, p.name)
            if descriptions:
                text.append('')
                text.extend(descriptions)
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
    data_name = reader_name.removesuffix('Reader') + 'Data'
    accessed_records = {
        access.identity
        for field in computed.values()
        if field.calculation
        for access in field.calculation.accesses
        if access.operation == 'records'
    }
    accessed_records.update(
        access.identity
        for definition in layout.model.definitions
        for constraint in definition.constraints
        for access in constraint.accesses
        if access.operation == 'records'
    )
    accessed_best = {
        access.identity
        for field in computed.values()
        if field.calculation
        for access in field.calculation.accesses
        if access.operation == 'best_available'
    }
    accessed_best.update(
        access.identity
        for definition in layout.model.definitions
        for constraint in definition.constraints
        for access in constraint.accesses
        if access.operation == 'best_available'
    )
    records = {
        key: names[s.root] + "".join(p.capitalize() for p in s.field.name.split("_")) + "Record"
        for key, s in layout.fields.items()
        if s.dimensions or s.field.category in ("snapshot", "scd") or key in accessed_records
    }
    selections = {
        key: names[s.root] + ''.join(p.capitalize() for p in s.field.name.split('_')) + 'Selection'
        for key, s in layout.fields.items()
        if layout.entities[s.root].source.origin != 'builtin' or key in accessed_best
    }
    dimension_types = {
        key: name.removesuffix('Selection') + 'Dimensions' for key, name in selections.items()
    }
    public_names = [
        *names.values(),
        *codes.values(),
        *records.values(),
        *selections.values(),
        *dimension_types.values(),
        *requests.values(),
        *value_names.values(),
        *(name + suffix for name in lookup_names.values() for suffix in ('Key', 'Matcher')),
        reader_name,
        data_name,
    ]
    reader_methods = [
        "get_" + snake(name) + suffix for name in names.values() for suffix in ("", "_many")
    ]
    if len(set(reader_methods)) != len(reader_methods) or "get_many" in reader_methods:
        raise ValueError("generated reader method collision")
    reserved = {
        "EntityHandle",
        "Reader",
        "DataStore",
        "TransactionReport",
        "TransactionStatus",
        "ReadResult",
        "EntityRef",
        "HierarchyView",
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
        "EntityValidationError",
        "Partial",
        "CalculationIssue",
        "Omitted",
        "UNSET",
        "PROGRAMS",
        "CONSTRAINT_PROGRAMS",
        "Mapping",
        "Sequence",
        "MODEL_DIGEST",
    }
    if lookups:
        reserved.update(
            {'AmbiguousMatch', 'LookupResult', 'LookupStats', 'Matcher', 'prepare_lookup'}
        )
    collisions = sorted(
        name for name in set(public_names) if public_names.count(name) > 1 or name in reserved
    )
    if collisions:
        location = next(
            (
                rule.source
                for identity, rule in lookups.items()
                if any(
                    lookup_names[identity] + suffix in collisions for suffix in ('Key', 'Matcher')
                )
            ),
            None,
        )
        prefix = f'{location.path}:{location.line}:{location.column}: ' if location else ''
        raise ValueError(
            prefix + "generated Python public-name collision: " + ", ".join(collisions)
        )
    lines = [
        '"""Generated typed data access and transaction API."""',
        "",
        "from __future__ import annotations",
        "",
        "from datetime import date, datetime",
        "from collections.abc import Mapping, Sequence",
        "from dataclasses import dataclass",
        "from decimal import Decimal",
        "from enum import StrEnum",
        "from pathlib import Path",
        "from typing import Self, cast",
        "",
        "from ._results import CalculationIssue, EntityRef, HierarchyView, NonNormalResult, Partial, ReadResult, Status, Success",
        "from ._computing import Omitted, UNSET",
        "from ._calculations import CONSTRAINT_PROGRAMS, PROGRAMS",
        "from ._runtime import EntityHandle, Reader",
        "from .transactions.repository import DataStore, TransactionReport, TransactionStatus",
        "from ._validation import EntityValidationError, ValidationReport, ValidationIssue",
        "",
    ]
    if lookups:
        lines.extend(
            [
                'from ._lookup import AmbiguousMatch, LookupResult, LookupStats, Matcher',
                'from ._lookup_prepare import prepare as prepare_lookup',
                '',
            ]
        )
    lines.append('from ._value_types import ' + ', '.join(sorted(value_names.values())))
    lines.extend(['', f'MODEL_DIGEST = {layout.model.model_digest!r}'])
    for target, name in sorted(codes.items()):
        dictionary = next((d for d in layout.model.definitions if d.identity == target), None)
        extensible = bool(
            dictionary
            and not dictionary.derived
            and not any(call.target == 'builtins.flags' for call in dictionary.decorators)
        )
        enum = bool(dictionary and (dictionary.entries or not extensible))
        lines.extend(
            [
                "",
                f'class {name}({"StrEnum" if enum else "str"}):',
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
            if extensible and dictionary.entries:
                lines.extend(
                    [
                        "",
                        "    @classmethod",
                        "    def _missing_(cls, value: object) -> Self | None:",
                        '        """Create an unregistered code; the reader validates membership."""',
                        "        if not isinstance(value, str):",
                        "            return None",
                        "        member = str.__new__(cls, value)",
                        "        object.__setattr__(member, '_name_', None)",
                        "        member._value_ = value",
                        "        return member",
                    ]
                )
    for lookup_identity, rule in lookups.items():
        name = lookup_names[lookup_identity]
        id_type = python_type(layout.ids[layout.roots[lookup_identity[0]]].type)
        lines.extend(
            ['', '', '@dataclass(frozen=True, slots=True, kw_only=True)', f'class {name}Key:']
        )
        lines.extend(
            docstring(
                [
                    f'Input key for {lookup_identity[0]}.{rule.name}.',
                    '',
                    *(
                        f':param {key.name}: Value matched against the {key.name} path.'
                        for key in rule.keys
                    ),
                ],
                4,
            )
        )
        lines.extend(f'    {key.name}: {python_type(key.type)}' for key in rule.keys)
        lines.extend(
            [
                '',
                '',
                f'class {name}Matcher(Matcher[{name}Key, {id_type}]):',
                f'    """Match {name}Key records to {lookup_identity[0]} IDs."""',
                '',
                f'    def match_many(self, keys: Sequence[{name}Key]) -> Success[tuple[LookupResult[{id_type}], ...]] | NonNormalResult:',
                *docstring(
                    [
                        'Match a batch against the prepared index.',
                        '',
                        ':param keys: Ordered key records; duplicates retain their positions.',
                        ':returns: Ordered matches or ERROR for an invalid batch.',
                        f':rtype: Success[tuple[LookupResult[{id_type}], ...]] | NonNormalResult',
                    ],
                    8,
                ),
                '        return super().match_many(keys)',
            ]
        )
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
    for identity, name in selections.items():
        stored = layout.fields[identity]
        dimensions_name = dimension_types[identity]
        lines.extend(
            [
                '',
                '',
                '@dataclass(frozen=True)',
                f'class {dimensions_name}:',
                *(
                    docstring(
                        [
                            f'Selected dimensions of {identity}.',
                            '',
                            *(
                                f':param {dimension.key}: Selected dimension value.'
                                for dimension in stored.dimensions
                            ),
                        ],
                        4,
                    )
                    if stored.dimensions
                    else [f'    """Selected dimensions of {identity}."""']
                ),
            ]
        )
        if stored.dimensions:
            lines.extend(
                f'    {dimension.key}: {python_type(dimension.type)}'
                for dimension in stored.dimensions
            )
        else:
            lines.append('    pass')
        lines.extend(
            [
                '',
                '',
                '@dataclass(frozen=True)',
                f'class {name}:',
                *docstring(
                    [
                        f'Selected value and dimensions of {identity}.',
                        '',
                        ':param value: Selected field value.',
                        ':param dimensions: Actual dimensions of the selected record.',
                    ],
                    4,
                ),
                f'    value: {python_type(stored.field.type)}',
                f'    dimensions: {dimensions_name}',
            ]
        )
    for identity, name in requests.items():
        field = computed[identity]
        reserved_parameters = {"self", "id_value", "best_effort"}
        if any(
            keyword.iskeyword(p.name) or p.name in reserved_parameters for p in contracts[identity]
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
        root_fields = [
            s.field
            for s in layout.fields.values()
            if s.root == key and entity.source.origin != "builtin"
        ]
        method_names = {"read_" + f.name + suffix for f in root_fields for suffix in ("", "_range")}
        for f in root_fields:
            if (
                f.name
                in {
                    "reader",
                    "entity_type",
                    "root_entity_type",
                    "read_entity_type",
                    "read_root_entity_type",
                    "id",
                    "_cache",
                    "_full",
                }
                | method_names
            ):
                raise ValueError(f"{f.identity}: generated property collision")
        for identity, field in computed.items():
            if roots[identity] != key:
                continue
            method = "read_" + field.name
            if method in method_names or {method, field.name} & {f.name for f in root_fields}:
                raise ValueError(f"{identity}: generated calculation method collision")
            methods.add(method)
            args = ", ".join([*parameters(field), "best_effort: bool = False"])
            values = "{" + ", ".join(f"{p.name!r}: {p.name}" for p in contracts[identity]) + "}"
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
            if stored.root != key or entity.source.origin == "builtin":
                continue
            f = stored.field
            output = python_type(f.type)
            dims = ", ".join(f"{d.key}: {python_type(d.type)}" for d in stored.dimensions)
            dimensions = "{" + ", ".join(f"{d.key!r}: {d.key}" for d in stored.dimensions) + "}"
            kw = ", *, " + dims if dims else ""
            method = "read_" + f.name
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
                f"tuple[{records[f.identity]}, ...]" if temporal or stored.dimensions else output
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
            declarations = {
                d.identity: d
                for definition in layout.model.definitions
                for d in definition.dimensions
            }
            selection_dimensions = [
                declarations[c.dimension_identity]
                for c in f.decorators
                if c.target == 'builtins.dimension'
            ]
            best_parameters: list[tuple[str, str, bool]] = []
            if temporal:
                best_parameters.append((time_key, 'date', False))
            for dimension in selection_dimensions:
                best_parameters.append(
                    (
                        dimension.value_name,
                        python_type(dimension.type),
                        bool(dimension.hierarchy_name),
                    )
                )
                if dimension.hierarchy_name:
                    best_parameters.append(
                        (
                            dimension.hierarchy_name,
                            'str | EntityRef[object] | HierarchyView',
                            True,
                        )
                    )
            for dimension in selection_dimensions:
                for dependency in dimension.hierarchy_inputs:
                    if dependency.name not in {item[0] for item in best_parameters}:
                        annotation = python_type(dependency.type)
                        if dependency.type.kind == 'reference':
                            annotation += ' | str'
                        best_parameters.append((dependency.name, annotation, True))
            args = ', '.join(
                name + ': ' + annotation + (' | Omitted = UNSET' if optional else '')
                for name, annotation, optional in best_parameters
            )
            values = (
                '{' + ', '.join(repr(name) + ': ' + name for name, _, _ in best_parameters) + '}'
            )
            best_method = method + '_best_available'
            if best_method in methods or best_method in method_names:
                raise ValueError(f'{key}: generated method collision {best_method}')
            methods.add(best_method)
            best_result = f'ReadResult[{selections[f.identity]}]'
            lines.extend(
                [
                    '',
                    f'    def {best_method}(self'
                    + (', *, ' + args if args else '')
                    + f') -> {best_result}:',
                    *docstring(
                        [
                            f'Select {f.identity} and return its value and actual dimensions.',
                            '',
                            *(
                                f':param {name}: Selection input ({annotation}).'
                                for name, annotation, _ in best_parameters
                            ),
                            ':returns: Selected value and dimensions, or a non-normal result.',
                            f':rtype: {best_result}',
                        ],
                        8,
                    ),
                    f'        return cast({best_result}, self.reader.read_best(self, {f.identity!r}, {values}))',
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
            "    selection_types = {"
            + ", ".join(f"{k!r}: ({v}, {dimension_types[k]})" for k, v in selections.items())
            + "}",
            "    entity_types = {" + ", ".join(f"{k!r}: {v}" for k, v in names.items()) + "}",
            "    code_types = {" + ", ".join(f"{k!r}: {v}" for k, v in codes.items()) + "}",
            "    value_types = {" + ", ".join(f"{k!r}: {v}" for k, v in value_names.items()) + "}",
            "    calculation_programs = PROGRAMS",
            "    constraint_programs = CONSTRAINT_PROGRAMS",
        ]
    )
    lookup_methods: set[str] = set()
    for entity_id, entity in layout.entities.items():
        for visible in entity.effective_lookups:
            lookup_identity = (visible.value, visible.name)
            rule = lookups[lookup_identity]
            name = lookup_names[lookup_identity]
            id_type = python_type(layout.ids[layout.roots[entity_id]].type)
            method = 'prepare_' + snake(names[entity_id]) + '_' + rule.name
            if method in lookup_methods:
                raise ValueError(
                    f'{rule.source.path}:{rule.source.line}:{rule.source.column}: generated reader method collision: {method}'
                )
            lookup_methods.add(method)
            args = ', '.join(
                f'{item.name}: {parameter_type(item)}'
                + (' = UNSET' if not item.required or item.default is not None else '')
                for item in rule.inputs
            )
            lookup_parameters = ', *, ' + args if args else ''
            values = '{' + ', '.join(f'{item.name!r}: {item.name}' for item in rule.inputs) + '}'
            input_docs = [
                line
                for item in rule.inputs
                for line in input_descriptions(rule.decorators, item.name)
            ]
            lines.extend(
                [
                    '',
                    f'    def {method}(self{lookup_parameters}) -> Success[{name}Matcher] | NonNormalResult:',
                    *docstring(
                        [
                            f'Prepare {rule.name} for {entity_id} and its descendants.',
                            '',
                            *input_docs,
                            *(
                                f':param {item.name}: Value for this preparation.'
                                for item in rule.inputs
                            ),
                            ':returns: A detached matcher, or ERROR if preparation fails.',
                            f':rtype: Success[{name}Matcher] | NonNormalResult',
                        ],
                        8,
                    ),
                    f'        _lookup_rule = next(rule for rule in self._layout.entities[{lookup_identity[0]!r}].lookups if rule.name == {rule.name!r})',
                    f'        return prepare_lookup(self._session, {entity_id!r}, _lookup_rule, {name}Key, {id_type}, {values}, {name}Matcher)',
                ]
            )
    for identity, name in requests.items():
        field = computed[identity]
        owner = abstract_owners.get(identity, field.implementation.rsplit(".", 1)[0])
        method = "read_" + snake(names[owner]) + "_" + field.name + "_many"
        result = f"ReadResult[tuple[ReadResult[{python_type(field.type)}], ...]]"
        values = "{" + ", ".join(f"{p.name!r}: request.{p.name}" for p in contracts[identity]) + "}"
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
            "",
            f"class {data_name}(DataStore):",
            '    """Open configured data stores and apply or roll back transactions."""',
            f"    reader_type = {reader_name}",
            "    schema_path = Path(__file__).with_name('schema.sql')",
            "",
            f"    def read(self) -> {reader_name}:",
            *docstring(
                [
                    "Create a consistent typed read session.",
                    "",
                    ":returns: Reader whose data remains stable across later commits.",
                    f":rtype: {reader_name}",
                ],
                8,
            ),
            f"        return cast({reader_name}, super().read())",
            "",
            "__all__ = "
            + repr(
                sorted(
                    [
                        *public_names,
                        "EntityRef",
                        "HierarchyView",
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
                        "EntityValidationError",
                        "TransactionReport",
                        "TransactionStatus",
                        "MODEL_DIGEST",
                        *(['AmbiguousMatch', 'LookupResult', 'LookupStats'] if lookups else []),
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
    dependencies = [
        requirement
        for requirement in requires('yambl-compiler') or ()
        if re.match(r'^(?:lclang|lark)\b', requirement)
    ]
    if any(
        sum(bool(re.match(r'^' + name + r'\b', d)) for d in dependencies) != 1
        for name in ('lclang', 'lark')
    ):
        raise ValueError('compiler package must declare one lclang and one lark runtime dependency')
    contents = {
        'pyproject.toml': (
            '[build-system]\nrequires = ["setuptools>=77"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            f'[project]\nname = {json.dumps(package.replace("_", "-"))}\n'
            f'version = {json.dumps(model.compiler_version)}\n'
            'requires-python = ">=3.14"\n'
            f'dependencies = {json.dumps(dependencies)}\n\n'
            f'[tool.setuptools.packages.find]\ninclude = ["{package}*"]\n\n'
            f'[tool.setuptools.package-data]\n{package} = ["*.json", "*.sql", "py.typed", "grammars/*.lark"]\n'
        ),
        "schema.sql": ddl(layout),
        "schema.md": reference(layout)
        + "\n## Table relationships\n\n[PlantUML source](schema-relations.puml)\n\n```plantuml\n"
        + relations(layout)
        + "```\n",
        "schema-relations.puml": relations(layout),
        "python-schema.md": python_reference(source, package),
        "storage-manifest.json": manifest(layout),
        package + "/__init__.py": source,
        package + '/schema.sql': ddl(layout),
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
        "scalars",
        "lookup",
        "lookup_prepare",
        "runtime",
        "validation",
        "relationship_validation",
        "entity_validation",
    ]
    for name in [*modules, "ir_model"]:
        path = (
            Path(__file__).with_name(name + ".py")
            if name != "ir_model"
            else Path(__file__).parents[1] / "ir/model.py"
        )
        text = path.read_text(encoding="utf-8").replace("yambl_compiler.ir.model", "._ir_model")
        text = text.replace(
            "yambl_compiler.languages.yambl.computation.records", "._computation_records"
        )
        text = text.replace('yambl_compiler.languages.yambl.computation.evaluation', '._evaluation')
        for module in modules:
            text = text.replace("yambl_compiler.storage." + module, "._" + module)
        contents[package + "/_" + name + ".py"] = text
    for path in sorted((Path(__file__).parents[1] / 'transactions').glob('*.py')):
        text = path.read_text(encoding='utf-8')
        text = text.replace('yambl_compiler.ir.model', '.._ir_model')
        text = text.replace('yambl_compiler.transactions.', '.')
        text = text.replace('yambl_compiler.languages.', '..languages.')
        for module in modules:
            text = text.replace('yambl_compiler.storage.' + module, '.._' + module)
        contents[package + '/transactions/' + path.name] = text
    language_root = Path(__file__).parents[1] / 'languages'
    contents[package + '/languages/__init__.py'] = (language_root / '__init__.py').read_text(
        encoding='utf-8'
    )
    for language in ('shared', 'yaplan', 'yatran'):
        for path in sorted((language_root / language).glob('*.py')):
            contents[package + '/languages/' + language + '/' + path.name] = path.read_text(
                encoding='utf-8'
            )
    for grammar in ('lexical', 'data', 'yaplan', 'yatran'):
        contents[package + '/grammars/' + grammar + '.lark'] = (
            Path(__file__).parents[1] / 'grammars' / (grammar + '.lark')
        ).read_text(encoding='utf-8')
    contents[package + "/_computation_records.py"] = (
        Path(__file__).parents[1] / "languages/yambl/computation/records.py"
    ).read_text(encoding="utf-8")
    contents[package + '/_evaluation.py'] = (
        (Path(__file__).parents[1] / 'languages/yambl/computation/evaluation.py')
        .read_text(encoding='utf-8')
        .replace('yambl_compiler.languages.yambl.computation.records', '._computation_records')
        .replace('yambl_compiler.storage.arithmetic', '._arithmetic')
        .replace('yambl_compiler.storage.conversions', '._conversions')
    )
    data_name = (
        "".join(word.capitalize() for word in package.removesuffix("_model").split("_")) + "Data"
    )
    contents[package + "/__main__.py"] = (
        f'"""Manage configured model data."""\nfrom . import {data_name}\nfrom .transactions.cli import main\n\nraise SystemExit(main({data_name}))\n'
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
