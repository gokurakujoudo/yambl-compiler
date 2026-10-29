"""Derive the offline JSON Schema from the typed IR wire contract."""

from collections.abc import Callable
from types import UnionType
from typing import Literal, cast, get_args, get_origin, get_type_hints

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from yambl_compiler.ir.model import Model, Record

type Schema = dict[str, object]


def schema() -> Schema:
    """Build a closed Draft 2020-12 schema with local references only.

    :returns: The current IR revision's schema.
    """
    definitions: dict[str, Schema] = {}

    def annotation(expected: object) -> Schema:
        """Translate one trusted annotation.

        :param expected: IR dataclass annotation.
        :returns: Corresponding JSON Schema fragment.
        """
        args = cast(tuple[object, ...], get_args(expected))
        origin = get_origin(expected)
        if origin is UnionType:
            return {'anyOf': [annotation(args[0]), {'type': 'null'}]}
        if origin is Literal:
            return {'type': 'string', 'enum': list(args)}
        if origin is tuple:
            return {'type': 'array', 'items': annotation(args[0])}
        if expected is str or expected is int or expected is bool:
            return {'type': {str: 'string', int: 'integer', bool: 'boolean'}[expected]}
        cls = cast(type[Record], expected)
        if cls.__name__ not in definitions:
            definitions[cls.__name__] = {}
            hints = cast(dict[str, object], get_type_hints(cls))
            properties = {name: annotation(value) for name, value in hints.items()}
            if cls is Model:
                properties['format_revision'] = {'type': 'integer', 'const': 9}
            definitions[cls.__name__] = {
                'type': 'object',
                'properties': properties,
                'required': list(hints),
                'additionalProperties': False,
            }
        return {'$ref': '#/$defs/' + cls.__name__}

    root = annotation(Model)
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        **root,
        '$defs': definitions,
    }


def validate_schema(value: object) -> None:
    """Validate wire structure without remote retrieval.

    :param value: Parsed JSON value.
    :returns: None when valid.
    :raises ValueError: On the first structural error, including its JSON path.
    """
    try:
        cast(
            Callable[[object], None],
            getattr(Draft202012Validator(schema()), 'validate'),
        )(value)
    except ValidationError as error:
        raise ValueError(f"{error.json_path}: {error.message}") from error
