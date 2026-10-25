"""Exact decimal arithmetic and explicit calculation result propagation."""

import math
from collections.abc import Callable, Sequence
from decimal import MAX_EMAX, MIN_EMIN, Decimal, DecimalException, localcontext
from typing import cast

from yambl_compiler.storage.results import (
    CalculationIssue,
    NonNormalResult,
    Partial,
    ReadResult,
    Status,
    Success,
)

type Number = int | Decimal | float


def failure(results: Sequence[NonNormalResult]) -> NonNormalResult:
    """Combine failed contributions without returning a misleading total.

    :param results: Nonempty failed contributions in deterministic order.
    :returns: Shared status, or ERROR for mixed statuses.
    """
    statuses = {r.status for r in results}
    status = next(iter(statuses)) if len(statuses) == 1 else Status.ERROR
    return NonNormalResult(
        status,
        "; ".join(r.message for r in results if r.message),
        tuple(
            i
            for r in results
            for i in (r.issues or (CalculationIssue(r.status, message=r.message),))
        ),
    )


def numeric(operator: str, left: Number, right: Number) -> Number:
    """Calculate with input-derived precision rather than ambient Decimal context.

    :param operator: One of ``+``, ``-`` or ``*``.
    :param left: Finite numeric operand, excluding bool.
    :param right: Finite numeric operand, excluding bool.
    :returns: Exact integer/decimal or finite float result.
    :raises ValueError: On unsupported operands, operators or nonfinite values.
    :raises DecimalException: On an unrepresentable Decimal result.
    """
    if type(left) not in (int, Decimal, float) or type(right) not in (
        int,
        Decimal,
        float,
    ):
        raise ValueError("arithmetic requires numeric operands, not bool")
    if operator not in ("+", "-", "*"):
        raise ValueError("unsupported calculation arithmetic operator")
    if isinstance(left, float) or isinstance(right, float):
        a, b = float(left), float(right)
        result = a + b if operator == "+" else a - b if operator == "-" else a * b
        if not all(math.isfinite(v) for v in (a, b, result)):
            raise ValueError("nonfinite floating-point calculation")
        return result
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        a_decimal, b_decimal = Decimal(left), Decimal(right)
        if not a_decimal.is_finite() or not b_decimal.is_finite():
            raise ValueError("nonfinite decimal calculation")
        a_tuple, b_tuple = a_decimal.as_tuple(), b_decimal.as_tuple()
        precision = (
            len(a_tuple.digits) + len(b_tuple.digits) + 1
            if operator == "*"
            else max(len(a_tuple.digits), len(b_tuple.digits))
            + abs(cast(int, a_tuple.exponent) - cast(int, b_tuple.exponent))
            + 2
        )
        with localcontext() as context:
            context.prec = precision
            context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
            return (
                a_decimal + b_decimal
                if operator == "+"
                else a_decimal - b_decimal if operator == "-" else a_decimal * b_decimal
            )
    return left + right if operator == "+" else left - right if operator == "-" else left * right


def lift(
    operation: Callable[..., object], values: Sequence[ReadResult[object]]
) -> ReadResult[object]:
    """Apply an operator to usable payloads and retain unhandled partial details.

    :param operation: Trusted generated scalar operation.
    :param values: Evaluated operand results.
    :returns: Normal, partial or failed result.
    """
    failed = [r for r in values if isinstance(r, NonNormalResult)]
    if failed:
        return failure(failed)
    payloads = [r.value for r in values if isinstance(r, (Success, Partial))]
    issues = tuple(i for r in values if isinstance(r, Partial) for i in r.issues)
    try:
        payload = operation(*payloads)
        return Partial(payload, issues) if issues else Success(payload, "computation")
    except (ValueError, TypeError, OverflowError, DecimalException) as error:
        return NonNormalResult(Status.ERROR, str(error))


def aggregate(
    values: Sequence[ReadResult[object]], best_effort: bool, zero: Number = 0
) -> ReadResult[object]:
    """Sum usable contributions under strict or opt-in best-effort rules.

    :param values: Contributions, including duplicate business rows.
    :param best_effort: Whether non-error failures may be omitted.
    :param zero: Correctly typed identity for empty input.
    :returns: Exact total, PARTIAL with omissions, or an unhandled failure.
    """
    failed = [r for r in values if isinstance(r, NonNormalResult)]
    usable = [r for r in values if isinstance(r, (Success, Partial))]
    if failed and (not best_effort or not usable or any(r.status == Status.ERROR for r in failed)):
        return failure(failed)
    issues = tuple(i for r in usable if isinstance(r, Partial) for i in r.issues)
    issues += tuple(
        i for r in failed for i in (r.issues or (CalculationIssue(r.status, message=r.message),))
    )
    try:
        total = zero
        for result in usable:
            total = numeric("+", total, cast(Number, result.value))
        return Partial(total, issues) if issues else Success(total, "computation")
    except (ValueError, TypeError, OverflowError, DecimalException) as error:
        return NonNormalResult(Status.ERROR, str(error))
