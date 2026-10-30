"""Validate scalar values with contracts detached from repository sessions."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import cast

from yambl_compiler.ir.model import Type, Value
from yambl_compiler.storage.conversions import convert
from yambl_compiler.storage.results import EntityRef


def finite_matches(member: Value, value: object) -> bool:
    """Compare a validated value with one typed finite-set member.

    :param member: Portable, already validated finite member.
    :param value: Decoded or canonically encoded scalar, or a structured value.
    :returns: Whether both values are equal under the declared type.
    """
    if member.kind == 'structured':
        return all(
            finite_matches(field.value, getattr(value, field.name)) for field in member.fields
        )
    if member.kind == 'reference' and isinstance(value, EntityRef):
        value = value.id
    if member.kind == 'bool':
        return bool(value) == (member.text == 'true')
    if member.kind in ('integer', 'snowflake_id'):
        return int(cast(str | int, value)) == int(member.text)
    if member.kind == 'decimal':
        return Decimal(cast(str | int | Decimal, value)) == Decimal(member.text)
    if member.kind == 'float':
        return float(cast(str | int | float | Decimal, value)) == float(member.text)
    if member.kind == 'date' and isinstance(value, date):
        return value.isoformat() == member.text
    if member.kind == 'datetime' and isinstance(value, datetime):
        return convert('format_datetime', (value,)) == member.text
    return str(value) == member.text


def check_finite(type_: Type, value: object) -> None:
    """Enforce an already validated finite contract at an input or decode boundary.

    :param type_: Declared value contract.
    :param value: Type-checked scalar or structured value.
    :returns: None when the value is a member.
    :raises ValueError: If the value is outside the allowed set.
    """
    if (
        type_.exact is not None
        and type_.kind != 'reference'
        and not finite_matches(type_.exact, value)
    ):
        raise ValueError('value violates exact constraint')
    if type_.allowed_values and not any(
        finite_matches(member, value) for member in type_.allowed_values
    ):
        raise ValueError('value violates one_of constraint')


def reference_matches(type_: Type, identity: object) -> bool:
    """Check a reference ID against its optional singleton constraint.

    :param type_: Reference contract.
    :param identity: Decoded or stored reference ID.
    :returns: Whether the ID satisfies the constraint.
    """
    exact = type_.exact
    if exact is None:
        return True
    expected = exact.target if exact.kind == 'dictionary' else exact.text
    return str(identity) == expected


def decode_scalar(
    type_: Type,
    value: object,
    code_type: type[str] | None = None,
    codes: frozenset[str] | None = None,
) -> object:
    """Decode one stored scalar and enforce its declared constraints.

    :param type_: Validated scalar contract.
    :param value: Encoded storage value.
    :param code_type: Generated dictionary class for a code.
    :param codes: Known dictionary members, or None for an external dictionary.
    :returns: Decoded value.
    :raises ValueError: If the payload violates its contract.
    """
    result: object
    if type_.name == 'bool':
        if type(value) is not int or value not in (0, 1):
            raise ValueError('invalid boolean payload')
        result = bool(value)
    else:
        if not isinstance(value, str):
            raise ValueError('expected TEXT payload')
        if type_.name in ('integer', 'snowflake_id'):
            result = int(value)
            if str(result) != value:
                raise ValueError('noncanonical integer')
        elif type_.name == 'decimal':
            result = Decimal(value)
            if not result.is_finite():
                raise ValueError('nonfinite decimal')
            params = {p.name: int(p.value) for p in type_.parameters}
            if params:
                _, digits, exponent = result.as_tuple()
                scale = max(0, -cast(int, exponent))
                whole = max(0, len(digits) + cast(int, exponent))
                if scale > params['scale'] or whole > params['precision'] - params['scale']:
                    raise ValueError('decimal exceeds declared precision or scale')
        elif type_.name == 'date':
            result = date.fromisoformat(value)
            if result.isoformat() != value:
                raise ValueError('noncanonical date')
        elif type_.name == 'datetime':
            result = convert('parse_datetime', (value,))
            if convert('format_datetime', (result,)) != value:
                raise ValueError('noncanonical datetime')
        elif type_.name == 'code':
            target = type_.arguments[0].target
            if codes is not None and value not in codes:
                raise ValueError(f'unknown code for {target}')
            assert code_type is not None
            result = code_type(value)
        else:
            result = value
    check_finite(type_, result)
    return result


def encode_input(type_: Type, value: object, code_type: type[str] | None = None) -> str | int:
    """Check a caller's scalar type and encode it for storage validation.

    :param type_: Validated scalar contract.
    :param value: Caller-supplied value.
    :param code_type: Generated dictionary class for a code.
    :returns: Encoded value.
    :raises ValueError: If the input has the wrong Python type.
    """
    if type_.name == 'bool':
        valid = type(value) is bool
        encoded: str | int = int(cast(bool, value)) if valid else 0
    elif type_.name in ('integer', 'snowflake_id'):
        valid = type(value) is int
        encoded = str(value)
    elif type_.name == 'decimal':
        valid = isinstance(value, Decimal)
        encoded = str(value)
    elif type_.name == 'date':
        valid = type(value) is date
        encoded = cast(date, value).isoformat() if valid else ''
    elif type_.name == 'datetime':
        valid = isinstance(value, datetime)
        encoded = cast(str, convert('format_datetime', (value,))) if valid else ''
    elif type_.name == 'code':
        assert code_type is not None
        valid = isinstance(value, code_type)
        encoded = str(value)
    else:
        valid = type(value) is str
        encoded = str(value)
    if not valid:
        raise ValueError(f'invalid input for {type_.name}')
    return encoded


@dataclass(frozen=True, slots=True)
class ScalarRule:
    """Validate a lookup input without a live storage session.

    :param value_type: Declared scalar or reference contract.
    :param code_type: Generated code class, if the scalar is a code.
    :param codes: Known dictionary entries, or None for external dictionaries.
    :param reference_id: Scalar ID contract for a reference, if applicable.
    """

    value_type: Type
    code_type: type[str] | None = None
    codes: frozenset[str] | None = None
    reference_id: Type | None = None

    def check(self, value: object) -> object:
        """Validate a typed input and return a detached composite-key value.

        :param value: External key field value.
        :returns: Validated value with canonical scalar semantics.
        :raises ValueError: If the value violates the contract.
        """
        if self.value_type.kind == 'reference':
            if not isinstance(value, EntityRef) or value.target != self.value_type.target:
                raise ValueError('dimension requires a reference of the declared type')
            assert self.reference_id is not None
            encoded = encode_input(self.reference_id, value.id)
            identity = decode_scalar(self.reference_id, encoded)
            if not reference_matches(self.value_type, identity):
                raise ValueError('reference violates exact constraint')
            return EntityRef[object](self.value_type.target, cast(str | int, identity))
        encoded = encode_input(self.value_type, value, self.code_type)
        return decode_scalar(self.value_type, encoded, self.code_type, self.codes)
