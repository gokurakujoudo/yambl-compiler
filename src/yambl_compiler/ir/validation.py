"""Validate embedded contracts and compare their derived semantic views."""

import re
from dataclasses import fields
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import cast

from yambl_compiler.ir.codec import JSON, IRValue, decode, encode, seal, semantic
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.model import Definition, Expression, Model, Record, Source, Value
from yambl_compiler.ir.restoring import restore
from yambl_compiler.syntax.parser import Diagnostic

REQUIRED = {
    'Decodes',
    'decodes_entry',
    'Hierarchy',
    'hierarchy_entry',
    'ConfigGroup',
    'AttrCats',
    'DecoratorTargets',
    'field_mapping',
    'targets',
    'allow_multiple',
    'title',
    'description',
    'temporal',
    'dimension',
    'flags',
    'override',
    'overload',
}
CONSTANTS = {
    'NOT_DEFINED',
    'NOT_APPLICABLE',
    'EMPTY',
    'NO_ENTRY',
    'NO_DATA',
    'ENTITY_NOT_FOUND',
    'ERROR',
}


def walk(value: IRValue, path: str = '$') -> list[tuple[str, Record]]:
    """Enumerate records with precise JSON diagnostic paths.

    :param value: Typed model value.
    :param path: Current JSON path.
    :returns: Located records.
    """
    result: list[tuple[str, Record]] = []
    if isinstance(value, Record):
        result.append((path, value))
        for field in fields(value):
            result.extend(walk(cast(IRValue, getattr(value, field.name)), path + '.' + field.name))
    elif isinstance(value, tuple):
        for i, item in enumerate(value):
            result.extend(walk(item, f'{path}[{i}]'))
    return result


def differences(expected: JSON, actual: JSON, path: str = '$') -> list[str]:
    """Locate derived data that disagrees with embedded declarations.

    :param expected: Stored semantic view.
    :param actual: Independently recomputed semantic view.
    :param path: Current JSON path.
    :returns: Paths whose values differ.
    """
    if isinstance(expected, dict) and isinstance(actual, dict) and expected.keys() == actual.keys():
        return [
            issue
            for key in expected
            for issue in differences(expected[key], actual[key], path + '.' + key)
        ]
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        return [
            issue
            for i, (left, right) in enumerate(zip(expected, actual, strict=True))
            for issue in differences(left, right, f'{path}[{i}]')
        ]
    return [] if expected == actual else [f'{path}: inconsistent semantic value']


def structural(model: Model) -> list[str]:
    """Check invariants required before invoking the semantic engine.

    :param model: Shape-checked model.
    :returns: Located structural problems.
    """
    issues: list[str] = []
    identifiers = [definition.identity for definition in model.definitions]
    if len(set(identifiers)) != len(identifiers):
        issues.append('$.definitions: duplicate qualified identity')
    builtin_names = {
        d.name.removeprefix('builtins.') for d in model.definitions if d.source.origin == 'builtin'
    }
    if not REQUIRED <= builtin_names:
        issues.append('$.definitions: incomplete embedded builtins')
    if {c.name for c in model.constants} != CONSTANTS or len(model.constants) != len(CONSTANTS):
        issues.append('$.constants: expected the seven result constants')
    if model.prefix and not re.fullmatch(
        r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*', model.prefix
    ):
        issues.append('$.prefix: invalid repository prefix')
    for path, item in walk(model):
        if isinstance(item, Source):
            source_path = PurePosixPath(item.path)
            if (
                not item.path
                or source_path.is_absolute()
                or any(p in item.path for p in ('\\', ':'))
                or '..' in source_path.parts
                or source_path.as_posix() != item.path
            ):
                issues.append(path + ': expected a relative POSIX source path')
            if min(item.line, item.column, item.end_line, item.end_column) < 1 or (
                item.end_line,
                item.end_column,
            ) < (item.line, item.column):
                issues.append(path + ': invalid source span')
            if item.namespace and not re.fullmatch(
                r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*', item.namespace
            ):
                issues.append(path + ': invalid lexical namespace')
            if item.origin == 'builtin' and item.namespace != 'builtins':
                issues.append(path + ': builtins require namespace builtins')
        elif isinstance(item, Definition):
            identity = (
                'model::'
                if item.source.origin == 'model' and item.source.namespace == 'builtins'
                else ''
            ) + item.name
            if item.identity != identity:
                issues.append(path + '.identity: inconsistent declaration identity')
            if item.name != '.'.join(
                filter(None, (item.source.namespace, item.name.split('.')[-1]))
            ):
                issues.append(path + '.name: identity disagrees with lexical namespace')
            if (item.kind == 'type_alias') != (item.expression is not None):
                issues.append(path + '.expression: only aliases require a body')
            if item.parent and item.kind != 'entity':
                issues.append(path + '.parent: only entities inherit')
            if item.parameters and item.kind not in ('type_alias', 'decorator'):
                issues.append(path + '.parameters: unsupported declaration parameters')
            for i, entry in enumerate(item.entries):
                if not re.fullmatch(r'-?(?:0|[1-9][0-9]*)', entry.ordering):
                    issues.append(f'{path}.entries[{i}].ordering: invalid integer')
        elif isinstance(item, Expression):
            if (item.kind == 'literal') != (item.literal is not None):
                issues.append(path + '.literal: invalid literal discriminator')
            if item.literal and item.literal.kind not in ('string', 'integer', 'decimal', 'bool'):
                issues.append(path + '.literal: expression requires a scalar literal')
            if (item.kind in ('reference', 'external')) != bool(item.target):
                issues.append(path + '.target: invalid resolved reference')
            if item.kind == 'reference' and item.target not in identifiers:
                issues.append(path + '.target: missing declaration ' + item.target)
            external_identity = (
                'model::'
                if item.source.origin == 'model' and item.source.namespace == 'builtins'
                else ''
            ) + '.'.join(filter(None, (item.source.namespace, item.name)))
            if item.kind == 'external' and item.target != external_identity:
                issues.append(path + '.target: invalid external dictionary identity')
        elif isinstance(item, Value):
            valid = True
            if item.kind == 'integer':
                valid = bool(re.fullmatch(r'-?(?:0|[1-9][0-9]*)', item.text))
            elif item.kind == 'decimal':
                try:
                    valid = Decimal(item.text).is_finite()
                except InvalidOperation:
                    valid = False
            elif item.kind == 'bool':
                valid = item.text in ('true', 'false')
            if not valid:
                issues.append(path + '.text: invalid scalar value')
    return issues


def located(model: Model, diagnostic: Diagnostic) -> str:
    """Attach a checker diagnostic to the most specific retained source record.

    :param model: Embedded model supplying source spans.
    :param diagnostic: Shared semantic diagnostic.
    :returns: JSON path and original source diagnostic.
    """
    matches = [
        (path.removesuffix('.source'), item)
        for path, item in walk(model)
        if isinstance(item, Source)
        and item.path == diagnostic.path.as_posix()
        and (item.line, item.column)
        <= (diagnostic.line, diagnostic.column)
        <= (item.end_line, item.end_column)
    ]
    path = (
        min(
            matches,
            key=lambda pair: (
                pair[1].end_line - pair[1].line,
                pair[1].end_column - pair[1].column,
                -len(pair[0]),
            ),
        )[0]
        if matches
        else '$.definitions'
    )
    return f'{path}: {diagnostic}'


def validate_ir(model: Model) -> tuple[str, ...]:
    """Validate a model without reading source or installed built-in definitions.

    :param model: Candidate intermediate model.
    :returns: JSON-path diagnostics, empty on success.
    """
    try:
        decode(encode(model), Model)
        if model.format_revision != 2:
            return ('$.format_revision: unsupported IR revision; recompile the model repository',)
        issues = structural(model)
        if issues:
            return tuple(issues)
        analysis = restore(model)
        errors = [located(model, d) for d in analysis.diagnostics if d.severity == 'error']
        if errors:
            return tuple(errors)
        rebuilt = Lowering(analysis, Path('.'), model.prefix, model.compiler_version).model()
        issues.extend(differences(semantic(encode(model)), semantic(encode(rebuilt))))
        issues.extend(
            differences(
                encode(model.external_dictionaries),
                encode(rebuilt.external_dictionaries),
                '$.external_dictionaries',
            )
        )
        sealed = seal(model)
        for name in ('builtins_digest', 'model_digest'):
            if getattr(model, name) != getattr(sealed, name):
                issues.append(f'$.{name}: content digest mismatch')
        return tuple(issues)
    except (ValueError, KeyError, IndexError, RecursionError) as error:
        return (f'$: invalid embedded model: {error}',)
