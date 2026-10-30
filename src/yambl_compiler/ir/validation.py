"""Validate embedded contracts and compare their derived semantic views."""

import json
import math
import re
from dataclasses import fields
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import cast

from yambl_compiler.ir.codec import JSON, IRValue, decode, encode, seal, semantic
from yambl_compiler.ir.lowering import Lowering
from yambl_compiler.ir.model import (
    Definition,
    Expression,
    Field,
    LookupBinding,
    Model,
    Record,
    Source,
    Type,
    Value,
)
from yambl_compiler.ir.restoring import restore
from yambl_compiler.languages.yambl.computation.records import (
    CalculationExpression,
    CalculationSpan,
    TypeHint,
)
from yambl_compiler.languages.yambl.semantics.expressions import REGEX_REFERENCE
from yambl_compiler.languages.yambl.syntax.parser import Diagnostic

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
    'unique_by',
    'title',
    'description',
    'temporal',
    'dimension',
    'decodes_rank',
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
    "PARTIAL",
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
            result.extend(walk(item, f"{path}[{i}]"))
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
            for issue in differences(left, right, f"{path}[{i}]")
        ]
    return [] if expected == actual else [f"{path}: inconsistent semantic value"]


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
        issues.append("$.constants: expected the eight result constants")
    if model.prefix and not re.fullmatch(
        r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*', model.prefix
    ):
        issues.append('$.prefix: invalid repository prefix')
    for path, item in walk(model):
        if isinstance(item, CalculationSpan):
            if (
                item.start < 0
                or item.end < item.start
                or min(item.line, item.column, item.end_line, item.end_column) < 1
                or (item.end_line, item.end_column) < (item.line, item.column)
            ):
                issues.append(path + ": invalid calculation span")
        elif isinstance(item, CalculationExpression):
            arity = {
                "name": 0,
                "string": 0,
                "integer": 0,
                "decimal": 0,
                "bool": 0,
                "member": 1,
                "descriptor": 1,
                "call": 1,
                "unary": 1,
                "binary": 2,
                "conditional": 3,
                "lambda": 1,
                "recover": 1,
                'sequence': len(item.operands),
                'object': len(item.operands) if len(item.operands) % 2 == 0 else -1,
            }[item.kind]
            if len(item.operands) != arity:
                issues.append(path + ".operands: invalid calculation operand count")
            if item.arguments and item.kind != "call":
                issues.append(path + ".arguments: only calls accept arguments")
            if item.cases and item.kind != "recover":
                issues.append(path + ".cases: only recover accepts cases")
            if item.kind == "unary" and item.value not in ("+", "-", "not"):
                issues.append(path + ".value: unsupported unary operator")
            if item.kind == "binary" and item.value not in (
                "??",
                "+",
                "-",
                "*",
                "/",
                "//",
                "and",
                "or",
                "==",
                "!=",
                "<",
                "<=",
                ">",
                ">=",
            ):
                issues.append(path + ".value: unsupported binary operator")
            if item.kind in ("integer", "decimal"):
                try:
                    valid = Decimal(item.value).is_finite() and (
                        item.kind != "integer" or bool(re.fullmatch(r"[0-9]+", item.value))
                    )
                except InvalidOperation:
                    valid = False
                if not valid:
                    issues.append(path + ".value: invalid calculation number")
            if item.kind == "bool" and item.value not in ("true", "false"):
                issues.append(path + ".value: invalid calculation boolean")
        elif isinstance(item, LookupBinding):
            if (bool(item.input_name) and bool(item.fixed_kind)) or not (
                item.input_name or item.fixed_kind or item.expression
            ):
                issues.append(path + ': expected an input, fixed value, or expression')
            if item.fixed_kind != 'code' and item.fixed_target:
                issues.append(path + '.fixed_target: only codes have a dictionary target')
            if item.fixed_kind == 'code' and not item.fixed_target:
                issues.append(path + '.fixed_target: code requires a dictionary target')
        elif isinstance(item, Field):
            if item.abstract and (item.category != 'computed' or item.calculation is not None):
                issues.append(
                    path + '.abstract: abstract fields require computed category and no body'
                )
            if not item.abstract and (item.category == "computed") != (
                item.calculation is not None
            ):
                issues.append(
                    path + ".calculation: only computed fields require a calculation body"
                )
        elif isinstance(item, Source):
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
            if item.abstract and (item.kind != 'entity' or item.final):
                issues.append(path + '.abstract: only non-final entities may be abstract')
            if item.derived and item.kind != 'decodes':
                issues.append(path + '.derived: only Decodes may be derived')
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
            if (item.kind in ('type_alias', 'constant')) != (item.expression is not None):
                issues.append(path + '.expression: aliases and constants require a type expression')
            if (item.kind == 'constant') != (item.initializer is not None):
                issues.append(path + '.initializer: only constants require an initializer')
            if item.references and item.kind != 'constant':
                issues.append(path + '.references: only constants carry initializer references')
            if item.parent and item.kind != 'entity':
                issues.append(path + '.parent: only entities inherit')
            if item.parameters and item.kind not in ('type_alias', 'decorator'):
                issues.append(path + '.parameters: unsupported declaration parameters')
            if item.unique_keys and (
                item.kind != 'entity'
                or any(
                    key.entity != item.identity
                    or not key.fields
                    or len(set(key.fields)) != len(key.fields)
                    for key in item.unique_keys
                )
            ):
                issues.append(path + '.unique_keys: invalid unique key')
            variadic = [
                index for index, parameter in enumerate(item.parameters) if parameter.variadic
            ]
            if variadic and (
                item.kind != 'decorator'
                or variadic != [len(item.parameters) - 1]
                or any(parameter.pair for parameter in item.parameters)
                or item.parameters[-1].optional
                or item.parameters[-1].default is not None
            ):
                issues.append(path + '.parameters: invalid variadic parameter')
            for i, entry in enumerate(item.entries):
                if not re.fullmatch(r'-?(?:0|[1-9][0-9]*)', entry.ordering):
                    issues.append(f"{path}.entries[{i}].ordering: invalid integer")
        elif isinstance(item, TypeHint):
            if item.name in ('one_of', 'exact') and item.finite_candidates:
                keys = [
                    json.dumps(encode(value), sort_keys=True, ensure_ascii=False)
                    for value in item.finite_values
                ]
                minimum = 1 if item.name == 'exact' else 2
                if (
                    len(item.finite_candidates) < minimum
                    or len(keys) < minimum
                    or keys != sorted(set(keys))
                ):
                    issues.append(path + ': invalid finite annotation')
            elif item.finite_candidates or item.finite_values:
                issues.append(path + ': finite values require a finite annotation')
        elif isinstance(item, Expression):
            names = {
                match.group(2)
                for parameter in item.parameters
                if item.kind == 'primitive' and item.name == 'string' and parameter.name == 'regex'
                for match in REGEX_REFERENCE.finditer(parameter.value)
                if not len(match.group(1)) % 2
            }
            if item.kind != 'finite' and (
                names != {reference.name for reference in item.references}
                or len(names) != len(item.references)
            ):
                issues.append(path + '.references: invalid string format bindings')
            if any(reference.value not in identifiers for reference in item.references):
                issues.append(path + '.references: missing string format declaration')
            if (item.kind == 'literal') != (item.literal is not None):
                issues.append(path + '.literal: invalid literal discriminator')
            if item.literal and item.literal.kind not in (
                'string',
                'integer',
                'decimal',
                'bool',
            ):
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
            if item.kind == 'finite':
                minimum = 1 if item.name == 'exact' else 2
                if (
                    item.name not in ('one_of', 'exact')
                    or len(item.finite_candidates) < minimum
                    or len(item.finite_values) < minimum
                    or len(item.finite_values) > len(item.finite_candidates)
                    or (item.name == 'exact' and len(item.finite_candidates) != 1)
                ):
                    issues.append(path + ': invalid finite expression')
                keys = [
                    json.dumps(encode(value), sort_keys=True, ensure_ascii=False)
                    for value in item.finite_values
                ]
                if keys != sorted(set(keys)):
                    issues.append(path + '.finite_values: expected unique canonical ordering')
                if len({reference.name for reference in item.references}) != len(item.references):
                    issues.append(path + '.references: duplicate finite expression binding')
            elif item.finite_candidates or item.finite_values:
                issues.append(path + ': finite values require a finite expression')
        elif isinstance(item, Type):
            if item.allowed_values:
                if (
                    item.exact is not None
                    or len(item.allowed_values) < 2
                    or item.kind in ('reference', 'collection', 'unknown')
                ):
                    issues.append(path + '.allowed_values: invalid finite constraint')
                expected = item.name if not item.target else 'structured'
                for value in item.allowed_values:
                    target = (
                        item.arguments[0].target
                        if item.name == 'code' and item.arguments
                        else item.target
                    )
                    if value.kind != expected or value.target != target:
                        issues.append(
                            path + '.allowed_values: member type disagrees with constraint'
                        )
                keys = [
                    json.dumps(encode(value), sort_keys=True, ensure_ascii=False)
                    for value in item.allowed_values
                ]
                if keys != sorted(set(keys)):
                    issues.append(path + '.allowed_values: expected unique canonical ordering')
        elif isinstance(item, Value):
            valid = True
            if item.kind in ('integer', 'snowflake_id'):
                valid = bool(re.fullmatch(r'-?(?:0|[1-9][0-9]*)', item.text))
            elif item.kind == 'decimal':
                try:
                    valid = Decimal(item.text).is_finite()
                except InvalidOperation:
                    valid = False
            elif item.kind == 'bool':
                valid = item.text in ('true', 'false')
            elif item.kind == 'float':
                try:
                    valid = math.isfinite(float(item.text))
                except ValueError:
                    valid = False
            elif item.kind == 'date':
                try:
                    valid = date.fromisoformat(item.text).isoformat() == item.text
                except ValueError:
                    valid = False
            elif item.kind == 'datetime':
                from yambl_compiler.storage.conversions import convert

                try:
                    parsed = convert('parse_datetime', (item.text,))
                    valid = convert('format_datetime', (parsed,)) == item.text
                except ValueError:
                    valid = False
            elif item.kind == 'structured':
                field_names = [field.name for field in item.fields]
                valid = (
                    bool(item.target) and not item.text and field_names == sorted(set(field_names))
                )
            if item.kind != 'structured' and item.fields:
                valid = False
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
    return f"{path}: {diagnostic}"


def validate_ir(model: Model) -> tuple[str, ...]:
    """Validate a model without reading source or installed built-in definitions.

    :param model: Candidate intermediate model.
    :returns: JSON-path diagnostics, empty on success.
    """
    try:
        decode(encode(model), Model)
        if model.format_revision != 17:
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
                issues.append(f"$.{name}: content digest mismatch")
        return tuple(issues)
    except (ValueError, KeyError, IndexError, RecursionError) as error:
        return (f"$: invalid embedded model: {error}",)
