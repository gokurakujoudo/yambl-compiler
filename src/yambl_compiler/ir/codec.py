"""Encode typed IR records and reject malformed or ambiguous JSON."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import fields, replace
from types import UnionType
from typing import Literal, cast, get_args, get_origin, get_type_hints

from yambl_compiler.ir.model import Model, Record
from yambl_compiler.ir.schema import validate_schema

type JSON = None | str | int | bool | list[JSON] | dict[str, JSON]
type IRValue = None | str | int | bool | Record | tuple[IRValue, ...]


def encode(value: IRValue) -> JSON:
    """Encode only the explicit IR record family and scalar payloads.

    :param value: Typed IR value.
    :returns: JSON-compatible values.
    """
    if isinstance(value, Record):
        return {f.name: encode(cast(IRValue, getattr(value, f.name))) for f in fields(value)}
    if isinstance(value, tuple):
        return [encode(item) for item in value]
    return value


def decode(value: JSON, expected: object, path: str = '$') -> IRValue:
    """Validate every field against the explicit dataclass wire contract.

    :param value: Untrusted JSON value.
    :param expected: Trusted IR annotation, never supplied by the input.
    :param path: JSON diagnostic path.
    :returns: Checked scalar, tuple or IR record.
    :raises ValueError: If a field, discriminator or value has the wrong shape.
    """
    arguments = cast(tuple[object, ...], get_args(expected))
    origin = get_origin(expected)
    if origin is UnionType:
        if value is None:
            return None
        return decode(value, arguments[0], path)
    if origin is Literal:
        if type(value) is not str or value not in arguments:
            raise ValueError(f'{path}: unsupported discriminator {value!r}')
        return value
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError(f'{path}: expected an array')
        return tuple(decode(item, arguments[0], f'{path}[{i}]') for i, item in enumerate(value))
    if expected is str or expected is int or expected is bool:
        if type(value) is not expected:
            raise ValueError(f'{path}: expected {cast(type[object], expected).__name__}')
        return value
    cls = cast(type[Record], expected)
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected an object')
    annotations = cast(dict[str, object], get_type_hints(cls))
    if set(value) != set(annotations):
        raise ValueError(
            f'{path}: missing fields {sorted(set(annotations) - set(value))}; '
            f'unknown fields {sorted(set(value) - set(annotations))}'
        )
    members = {
        name: decode(value[name], annotation, f'{path}.{name}')
        for name, annotation in annotations.items()
    }
    return cast(Callable[..., Record], cls)(**members)


def object_pairs(pairs: list[tuple[str, JSON]]) -> dict[str, JSON]:
    """Reject duplicate keys instead of silently overwriting a definition.

    :param pairs: JSON object members in input order.
    :returns: Unique object mapping.
    :raises ValueError: If a member occurs more than once.
    """
    result: dict[str, JSON] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'$: duplicate JSON key: {key}')
        result[key] = value
    return result


def nonstandard_number(value: str) -> None:
    """Reject NaN and infinity spellings accepted by the default JSON decoder.

    :param value: Invalid numeric spelling.
    :returns: Never returns.
    :raises ValueError: Always, because these are not JSON numbers.
    """
    raise ValueError(f'$: nonstandard JSON number: {value}')


def loads(text: str) -> Model:
    """Decode an experimental IR without reading any language resources.

    :param text: UTF-8 decoded artifact.
    :returns: Shape-checked model; semantic validation is a separate step.
    :raises ValueError: If JSON, shape or revision is unsupported.
    """
    data = cast(
        JSON, json.loads(text, object_pairs_hook=object_pairs, parse_constant=nonstandard_number)
    )
    if (
        not isinstance(data, dict)
        or type(data.get('format_revision')) is not int
        or data['format_revision'] != 2
    ):
        raise ValueError(
            '$.format_revision: unsupported IR revision; recompile the model repository'
        )
    validate_schema(data)
    return cast(Model, decode(data, Model))


def dumps(model: Model) -> str:
    """Serialize an artifact deterministically with exactly one trailing LF.

    :param model: Typed intermediate model.
    :returns: Compact JSON text.
    """
    return (
        json.dumps(encode(model), ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
    )


def semantic(value: JSON) -> JSON:
    """Remove provenance and invocation spelling from semantic hashing.

    :param value: Encoded IR record or subtree.
    :returns: Semantic-only JSON data.
    """
    if isinstance(value, list):
        return [semantic(item) for item in value]
    if isinstance(value, dict):
        removed = {'source', 'sites', 'compiler_version', 'builtins_digest', 'model_digest'}
        if 'bound' in value:
            removed.add('arguments')
        return {key: semantic(item) for key, item in value.items() if key not in removed}
    return value


def digest(value: JSON) -> str:
    """Hash the canonical semantic representation.

    :param value: Encoded data.
    :returns: Lowercase SHA-256 digest.
    """
    payload = json.dumps(semantic(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def seal(model: Model) -> Model:
    """Compute both content digests without changing model semantics.

    :param model: Canonical model.
    :returns: Model containing fresh built-in and model digests.
    """
    builtins: JSON = {
        'primitives': encode(model.primitives),
        'constants': encode(model.constants),
        'definitions': encode(tuple(d for d in model.definitions if d.source.origin == 'builtin')),
    }
    return replace(model, builtins_digest=digest(builtins), model_digest=digest(encode(model)))
