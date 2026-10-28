import asyncio
import sys
from contextlib import closing
from pathlib import Path

from sample_model import ItemDoubledQuantityRequest, SampleData, Status


async def main():
    config = Path(sys.argv[1])
    plan = Path(sys.argv[2])
    parsed = SampleData.validate_plan_syntax(plan)
    with await SampleData.open(config) as data:
        data.validate_plan_expectations(parsed)
        with closing(data.read()) as old:
            item = old.get_item('item-1').value
            assert item.read_doubled_quantity().value == 6
            batch = old.read_item_doubled_quantity_many(
                [
                    ItemDoubledQuantityRequest(id_value='item-1'),
                    ItemDoubledQuantityRequest(id_value='item-2'),
                ]
            )
            assert [result.value for result in batch.value] == [6, 10]
            changed = data.apply(parsed)
            assert item.read_doubled_quantity().value == 6
            with closing(data.read()) as current:
                assert current.get_item('item-1').value.read_doubled_quantity().value == 8
                assert current.validate().value.valid
            undone = data.rollback(changed.path)
            assert undone.status.value == 'committed'
            with closing(data.read()) as restored:
                assert restored.get_item('item-1').value.read_doubled_quantity().value == 6
                assert restored.get_item('missing').status == Status.NO_ENTRY
            redone = data.rollback(undone.path)
            with closing(data.read()) as replayed:
                assert replayed.get_item('item-1').value.read_doubled_quantity().value == 8
            print(
                f'apply={changed.status.value}, rollback={undone.status.value}, redo={redone.status.value}'
            )


asyncio.run(main())
