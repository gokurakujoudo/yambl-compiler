```python
import asyncio
from datetime import date
from pathlib import Path
from sample_model import SampleData, PriceSourcesCode, Success

async def show_price(config_path: Path) -> None:
    with await SampleData.open(config_path) as data:
        with data.read() as reader:
            found = reader.get_security('BOND00000001')
            assert isinstance(found, Success), found
            bond = found.value
            print(bond.read_price(date(2026, 1, 2), price_source=PriceSourcesCode('Primary')))
            print(bond.read_notional_value_range(date(2026, 1, 1), date(2026, 1, 8)))
            print(bond.read_price_fallback_days())

asyncio.run(show_price(Path('@sqlite@')))
```

```python
import asyncio
from datetime import date
from pathlib import Path
from sample_model import SampleData, FundIndustryMarketValueRequest, Success

async def show_totals(config_path: Path) -> None:
    with await SampleData.open(config_path) as data:
        with data.read() as reader:
            result = reader.read_fund_industry_market_value_many([
                FundIndustryMarketValueRequest(
                    id_value='FUND00000001', as_of_date=date(2026, 1, 6),
                    price_hierarchy='LocalHierarchy',
                ),
                FundIndustryMarketValueRequest(
                    id_value='FUND00000001', as_of_date=date(2026, 1, 6),
                    industry_date=date(2026, 1, 5), price_hierarchy='LocalHierarchy',
                ),
            ])
            assert isinstance(result, Success), result
            print(result.value)

asyncio.run(show_totals(Path('@files@')))
```
