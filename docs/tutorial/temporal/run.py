import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from temporal_model import NonNormalResult, Partial, Status, Success, TemporalData


def show_result(result):
    if isinstance(result, Success):
        return f'value={result.value}'
    if isinstance(result, Partial):
        return f'partial={result.value}, issues={len(result.issues)}'
    return f'status={result.status.value}, message={result.message}'


async def main():
    config, initial, revise = map(Path, sys.argv[1:])
    with await TemporalData.create(config) as data:
        data.apply(initial)
        with data.read() as reader:
            item = reader.get_instrument('i1').value
            price = item.read_price(date(2026, 1, 5), venue='primary')
            assert isinstance(price, Success) and price.value == Decimal('10.5')
            missing = item.read_price(date(2026, 1, 6), venue='primary')
            assert isinstance(missing, NonNormalResult) and missing.status == Status.NO_ENTRY
            prices = item.read_price_range(date(2026, 1, 1), date(2026, 2, 1), venue='primary')
            assert isinstance(prices, Success) and [p[1].value for p in prices.value] == [
                Decimal('10.5'),
                Decimal('11.5'),
            ]
            assert item.read_price(date(2026, 1, 5), venue='secondary').value == Decimal('9.5')
            assert item.read_rating(date(2026, 1, 15)).value == 'A'
            assert item.read_rating(date(2026, 2, 1)).status == Status.NO_ENTRY
            history = item.read_rating_range(date(2026, 1, 1), date(2026, 3, 1))
            assert isinstance(history, Success)
            assert history.value[0][1].value == 'A'
            assert history.value[1][1].status == Status.NO_ENTRY
            print(show_result(price), show_result(missing))
            basket = reader.get_basket('b1').value
            strict = basket.read_total()
            assert isinstance(strict, NonNormalResult) and strict.status == Status.NO_ENTRY
            partial = basket.read_total(best_effort=True)
            assert isinstance(partial, Partial) and partial.value == Decimal('10')
            assert partial.issues[0].status == Status.NO_ENTRY
            print(show_result(strict), show_result(partial))
        missing_plan = TemporalData.validate_plan_syntax(revise.with_name('expect_fail.yaplan'))
        try:
            data.validate_plan_expectations(missing_plan)
        except ValueError as error:
            assert 'expect any matched 0' in str(error)
        else:
            raise AssertionError('missing scope must fail its expectation')
        with data.read() as reader:
            assert reader.get_instrument('i1').value.read_price(
                date(2026, 1, 5), venue='primary'
            ).value == Decimal('10.5')
        parsed = TemporalData.validate_plan_syntax(revise)
        data.validate_plan_expectations(parsed)
        changed = data.apply(parsed)
        with data.read() as reader:
            item = reader.get_instrument('i1').value
            assert item.read_price(date(2026, 1, 10), venue='primary').value == Decimal('12.5')
            assert item.read_rating(date(2026, 1, 15)).value == 'B'
        undone = data.rollback(changed.path, groups=('prices',))
        with data.read() as reader:
            item = reader.get_instrument('i1').value
            assert item.read_price(date(2026, 1, 5), venue='primary').value == Decimal('10.5')
            assert item.read_price(date(2026, 1, 10), venue='primary').status == Status.NO_ENTRY
            assert item.read_rating(date(2026, 1, 15)).value == 'B'
            report = reader.validate()
            if not isinstance(report, Success):
                raise RuntimeError(show_result(report))
            if not report.value.valid:
                raise ValueError(report.value.issues)
            print(f'partial rollback={undone.status.value}, valid={report.value.valid}')


asyncio.run(main())
