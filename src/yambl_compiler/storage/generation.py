"""Generate importable typed readers and publish complete output directories."""

import keyword
import re
import tempfile
from pathlib import Path

from yambl_compiler.ir.codec import dumps
from yambl_compiler.ir.model import Model, Type
from yambl_compiler.storage.layout import Layout, build_layout, snake
from yambl_compiler.storage.rendering import ddl, manifest, reference


def python_package(layout: Layout, package: str) -> str:
    """Render explicit typed methods delegating to the shared runtime.

    :param layout: Checked storage mappings.
    :param package: Python package name.
    :returns: Importable Python source.
    :raises ValueError: On generated public-name collisions.
    """
    names = {key: entity.name.rsplit('.', 1)[-1] for key, entity in layout.entities.items()}
    codes: dict[str, str] = {}

    def python_type(type_: Type) -> str:
        """Render a supported Python value annotation.

        :param type_: Resolved supported IR type.
        :returns: Python annotation.
        """
        if type_.kind == 'reference':
            return f'EntityRef[{names[type_.target]}]'
        if type_.name == 'collection':
            return f'frozenset[{python_type(type_.arguments[0])}]'
        if type_.name == 'code':
            target = type_.arguments[0].target
            codes[target] = target.rsplit('.', 1)[-1] + 'Code'
            return codes[target]
        return {
            'string': 'str',
            'bool': 'bool',
            'integer': 'int',
            'snowflake_id': 'int',
            'decimal': 'Decimal',
            'date': 'date',
        }[type_.name]

    for stored in layout.fields.values():
        if stored.root not in layout.entities:
            continue
        python_type(stored.field.type)
        for dim in stored.dimensions:
            python_type(dim.type)
    reader_name = (
        ''.join(word.capitalize() for word in package.removesuffix('_model').split('_')) + 'Reader'
    )
    public_names = [*names.values(), *codes.values(), reader_name]
    reader_methods = [
        'get_' + snake(name) + suffix for name in names.values() for suffix in ('', '_many')
    ]
    if len(set(reader_methods)) != len(reader_methods) or 'get_many' in reader_methods:
        raise ValueError('generated reader method collision')
    reserved = {
        'EntityHandle',
        'Reader',
        'ReadResult',
        'EntityRef',
        'Success',
        'NonNormalResult',
        'Status',
        'Path',
        'Decimal',
        'StrEnum',
        'date',
        'cast',
    }
    if len(set(public_names)) != len(public_names) or set(public_names) & reserved:
        raise ValueError('generated Python public-name collision')
    lines = [
        '"""Generated typed read-only model API."""',
        '',
        'from __future__ import annotations',
        '',
        'from datetime import date',
        'from decimal import Decimal',
        'from enum import StrEnum',
        'from pathlib import Path',
        'from typing import cast',
        '',
        'from yambl_compiler.storage.results import EntityRef, NonNormalResult, ReadResult, Status, Success',
        'from yambl_compiler.storage.runtime import EntityHandle, Reader',
        '',
    ]
    for target, name in sorted(codes.items()):
        dictionary = next((d for d in layout.model.definitions if d.identity == target), None)
        lines.extend(
            [
                '',
                f'class {name}({"StrEnum" if dictionary else "str"}):',
                f'    """Codes belonging to {target}."""',
            ]
        )
        if dictionary:
            for entry in dictionary.entries:
                if (
                    not entry.code.isidentifier()
                    or keyword.iskeyword(entry.code)
                    or entry.code in ('name', 'value')
                ):
                    raise ValueError(f'{target}.{entry.code}: unsupported Python enum member')
                lines.append(f'    {entry.code} = {entry.code!r}')
    ordered = sorted(layout.entities, key=lambda k: len(_ancestors(layout, k)))
    for key in ordered:
        entity = layout.entities[key]
        parent = names.get(entity.parent, 'EntityHandle')
        lines.extend(
            ['', '', f'class {names[key]}({parent}):', f'    """Read handle for {entity.name}."""']
        )
        if layout.roots[key] != key:
            continue
        methods: set[str] = set()
        for stored in layout.fields.values():
            if stored.root != key:
                continue
            f = stored.field
            output = python_type(f.type)
            dims = ', '.join(f'{d.key}: {python_type(d.type)}' for d in stored.dimensions)
            dimensions = '{' + ', '.join(f'{d.key!r}: {d.key}' for d in stored.dimensions) + '}'
            kw = ', *, ' + dims if dims else ''
            method = 'read_' + f.name
            if method in methods:
                raise ValueError(f'{key}: generated method collision {method}')
            methods.add(method)
            temporal = f.category in ('snapshot', 'scd')
            time_key = next(
                (b.key for c in f.decorators if c.target == 'builtins.temporal' for b in c.bound),
                'as_of_date',
            )
            if temporal and (
                keyword.iskeyword(time_key)
                or time_key in {'self', *(d.key for d in stored.dimensions)}
                or any(d.key in ('start_date', 'end_date') for d in stored.dimensions)
            ):
                raise ValueError(f'{f.identity}: generated time parameter collision')
            point_arg = f', {time_key}: date' if temporal else ''
            when = f', {time_key}' if temporal else ''
            lines.extend(
                [
                    '',
                    f'    def {method}(self{point_arg}{kw}) -> ReadResult[{output}]:',
                    f'        """Read {f.identity} explicitly."""',
                    f'        return cast(ReadResult[{output}], self.reader.read(self, {f.identity!r}, {dimensions}{when}))',
                ]
            )
            if temporal:
                range_method = method + '_range'
                if range_method in methods or any(
                    'read_' + s.field.name == range_method
                    for s in layout.fields.values()
                    if s.root == key
                ):
                    raise ValueError(f'{key}: generated range method collision')
                methods.add(range_method)
                result = f'ReadResult[list[tuple[date, ReadResult[{output}]]]]'
                lines.extend(
                    [
                        '',
                        f'    def {range_method}(self, start_date: date, end_date: date{kw}) -> {result}:',
                        f'        """Read {f.identity} in [start_date, end_date)."""',
                        f'        return cast({result}, self.reader.read_range(self, {f.identity!r}, {dimensions}, start_date, end_date))',
                    ]
                )
    lines.extend(
        [
            '',
            '',
            f'class {reader_name}(Reader):',
            '    """Open a matching database in read-only mode."""',
            "    model_path = Path(__file__).with_name('model.yambl-ir.json')",
            '    entity_types = {' + ', '.join(f'{k!r}: {v}' for k, v in names.items()) + '}',
            '    code_types = {' + ', '.join(f'{k!r}: {v}' for k, v in codes.items()) + '}',
        ]
    )
    for key, name in names.items():
        id_type = python_type(layout.ids[layout.roots[key]].type)
        method = 'get_' + snake(name)
        lines.extend(
            [
                '',
                f'    def {method}(self, id_value: {id_type}) -> ReadResult[{name}]:',
                f'        """Read one {key} or subtype identity."""',
                f'        return cast(ReadResult[{name}], self.get({key!r}, id_value))',
                '',
                f'    def {method}_many(self, ids: list[{id_type}]) -> ReadResult[list[ReadResult[{name}]]]:',
                '        """Read IDs in order, preserving duplicates."""',
                f'        return cast(ReadResult[list[ReadResult[{name}]]], self.get_many({key!r}, list(ids)))',
            ]
        )
    lines.extend(
        [
            '',
            '__all__ = '
            + repr(
                sorted(
                    [
                        *public_names,
                        'EntityRef',
                        'ReadResult',
                        'Success',
                        'NonNormalResult',
                        'Status',
                    ]
                )
            ),
            '',
        ]
    )
    return '\n'.join(lines)


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
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', package) or keyword.iskeyword(package):
        raise ValueError('package must be a non-keyword snake_case identifier')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('generate requires a new or empty output directory')
    layout = build_layout(model)
    contents = {
        'schema.sql': ddl(layout),
        'schema.md': reference(layout),
        'storage-manifest.json': manifest(layout),
        package + '/__init__.py': python_package(layout, package),
        package + '/py.typed': '',
        package + '/model.yambl-ir.json': dumps(model),
    }
    if not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent, prefix='.yambl-generate-') as temporary:
            stage = Path(temporary) / 'output'
            stage.mkdir()
            for name, text in contents.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding='utf-8', newline='\n')
            if output.exists():
                output.rmdir()
            stage.rename(output)
    return layout
