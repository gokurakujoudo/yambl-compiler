import asyncio
import sys
from contextlib import closing
from pathlib import Path

from sample_model import SampleData


async def main():
    with await SampleData.open(Path(sys.argv[1])) as data:
        with closing(data.read()) as reader:
            book = reader.get_item('item-1').value
            other = reader.get_item('item-3').value
            assert book.read_multiplier().value == 3
            assert book.read_multiplier().origin == 'config_group'
            assert book.read_doubled_quantity().value == 12
            assert str(other.read_category().value) == 'Other'
            assert other.read_doubled_quantity().value == 4
            print('book=12, other=4')


asyncio.run(main())
