"""Load portable generated contracts without a compiler or schema dependency."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from types import UnionType
from typing import Literal, cast, get_args, get_origin, get_type_hints

from yambl_compiler.storage.records import Layout

type JSON = None | str | int | bool | list[JSON] | dict[str, JSON]
RUNTIME_REVISION = 19


def unique_object(pairs: list[tuple[str, JSON]]) -> dict[str, JSON]:
    """Reject repeated keys in any JSON object.

    :param pairs: Decoded members in source order.
    :returns: Unique members.
    :raises ValueError: On duplicate keys.
    """
    result: dict[str, JSON] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_number(value: str) -> None:
    """Reject floating and nonstandard JSON numbers.

    :param value: Original numeric token.
    :returns: Never returns.
    :raises ValueError: Always; exact numbers use strings.
    """
    raise ValueError(f"unsupported JSON number: {value}")


def loads(text: str) -> JSON:
    """Read strict exact-number JSON.

    :param text: UTF-8 decoded JSON.
    :returns: JSON data without duplicate keys or floating values.
    :raises ValueError: On malformed JSON.
    """
    return cast(
        JSON,
        json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_number,
            parse_float=reject_number,
        ),
    )


def decode(value: JSON, expected: object, path: str = "$") -> object:
    """Decode only the trusted dataclass contract supplied by generated code.

    :param value: Untrusted JSON tree.
    :param expected: Trusted Python annotation.
    :param path: Diagnostic JSON path.
    :returns: Checked scalar, container or dataclass instance.
    :raises ValueError: On a shape, field or scalar mismatch.
    """
    origin, args = get_origin(expected), get_args(expected)
    if origin is UnionType:
        for option in args:
            if option is type(None) and value is None:
                return None
            if option is type(None):
                continue
            try:
                return decode(value, option, path)
            except ValueError:
                pass
        raise ValueError(f'{path}: invalid union value')
    if origin is Literal:
        if type(value) is not str or value not in args:
            raise ValueError(f"{path}: invalid discriminator")
        return value
    if expected is str or expected is int or expected is bool:
        if type(value) is not expected:
            raise ValueError(f"{path}: invalid scalar type")
        return value
    if origin in (tuple, list):
        if not isinstance(value, list):
            raise ValueError(f"{path}: expected array")
        fixed = origin is tuple and args[-1] is not Ellipsis
        if fixed and len(value) != len(args):
            raise ValueError(f"{path}: invalid tuple length")
        items = [
            decode(v, args[i] if fixed else args[0], f"{path}[{i}]") for i, v in enumerate(value)
        ]
        return tuple(items) if origin is tuple else items
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    if origin is dict:
        return {k: decode(v, args[1], path + "." + k) for k, v in value.items()}
    hints = get_type_hints(cast(type[object], expected))
    if set(value) != set(hints):
        raise ValueError(f"{path}: missing or unknown fields")
    return cast(Callable[..., object], expected)(
        **{k: decode(v, hints[k], path + "." + k) for k, v in value.items()}
    )


def dumps(layout: Layout) -> str:
    """Encode the checked physical and semantic contract deterministically.

    :param layout: Generator-validated model and layout.
    :returns: Compact JSON with one final newline.
    """
    return (
        json.dumps(
            {"runtime_revision": RUNTIME_REVISION, "layout": asdict(layout)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def load_contract(path: Path, digest: str) -> Layout:
    """Read the exact runtime contract pinned by the generated package.

    :param path: Bundled contract file.
    :param digest: SHA-256 recorded in generated Python.
    :returns: Typed layout, without compiling IR or consulting source files.
    :raises ValueError: On altered content or unsupported structure.
    :raises OSError: On file access failure.
    """
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("runtime contract digest does not match generated package")
    data = loads(content.decode("utf-8"))
    if (
        not isinstance(data, dict)
        or set(data) != {"runtime_revision", "layout"}
        or type(data["runtime_revision"]) is not int
        or data["runtime_revision"] != RUNTIME_REVISION
    ):
        raise ValueError("unsupported runtime contract revision or fields")
    return cast(Layout, decode(data["layout"], Layout))
