"""Generate immutable structured value classes used by calculation literals."""

import keyword

from yambl_compiler.ir.model import Model, Type


def code_type_name(target: str) -> str:
    """Name the generated Python code type for a dictionary identity.

    :param target: Qualified dictionary identity.
    :returns: Valid Python class name.
    :raises ValueError: If the identity cannot form a Python name.
    """
    local = target.rsplit('.', 1)[-1]
    if local.endswith('::subtypes'):
        local = local.removesuffix('::subtypes') + 'Subtypes'
    name = local + 'Code'
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f'{target}: unsupported Python code type name')
    return name


def generate_values(model: Model) -> str:
    """Generate parser-independent dataclasses with precise field annotations.

    :param model: Checked portable model.
    :returns: Python module source.
    :raises ValueError: For unsupported types or Python names.
    """
    definitions = {d.identity: d for d in model.definitions if d.kind == 'value'}
    imports: set[str] = set()

    def annotation(type_: Type) -> str:
        """Render a noncollection value field.

        :param type_: Resolved field contract.
        :returns: Precise Python annotation.
        :raises ValueError: For unsupported fields.
        """
        if type_.target in definitions:
            return definitions[type_.target].name.rsplit('.', 1)[-1]
        if type_.kind == 'reference':
            name = type_.target.rsplit('.', 1)[-1]
            imports.add(name)
            return f'EntityRef[{name}]'
        if type_.name == 'code':
            name = code_type_name(type_.arguments[0].target)
            imports.add(name)
            return name
        scalar = {
            'string': 'str',
            'integer': 'int',
            'snowflake_id': 'int',
            'decimal': 'Decimal',
            'float': 'float',
            'bool': 'bool',
            'date': 'date',
            'datetime': 'datetime',
        }
        if type_.name not in scalar:
            raise ValueError(f'unsupported structured value field: {type_.name}')
        return scalar[type_.name]

    body: list[str] = []
    names: set[str] = set()
    for definition in definitions.values():
        name = definition.name.rsplit('.', 1)[-1]
        if name in names or not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError(f'generated structured value name collision: {name}')
        names.add(name)
        body.extend(
            [
                '',
                '',
                '@dataclass(frozen=True)',
                f'class {name}:',
                f'    """Structured value {definition.name}.',
            ]
        )
        body.extend(f'\n    :param {f.name}: Value of {f.identity}.' for f in definition.fields)
        body.append('    """')
        for field in definition.fields:
            if not field.name.isidentifier() or keyword.iskeyword(field.name):
                raise ValueError(f'unsupported Python value field name: {field.identity}')
            body.append(f'    {field.name}: {annotation(field.type)}')
    header = [
        '"""Generated immutable structured values."""',
        'from __future__ import annotations',
        'from dataclasses import dataclass',
        'from datetime import date, datetime',
        'from decimal import Decimal',
        'from typing import TYPE_CHECKING',
        'from ._results import EntityRef',
    ]
    if imports:
        header.extend(['if TYPE_CHECKING:', '    from . import ' + ', '.join(sorted(imports))])
    else:
        header.remove('from typing import TYPE_CHECKING')
    for name, module in (
        ('date', 'datetime'),
        ('datetime', 'datetime'),
        ('Decimal', 'decimal'),
        ('EntityRef', '._results'),
    ):
        if name == 'date':
            header.remove('from datetime import date, datetime')
        if any(line.endswith(f': {name}') or f': {name}[' in line for line in body):
            if name in ('date', 'datetime'):
                header.insert(3, f'from {module} import {name}')
        elif name not in ('date', 'datetime'):
            header.remove(f'from {module} import {name}')
    return '\n'.join(header + body) + '\n'
