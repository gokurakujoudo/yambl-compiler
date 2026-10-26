"""Exercise the actual FMC example without consulting model sources at read time."""

import asyncio
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.formatting import format_document
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success


class FmcTests(FilesystemCase):
    def test_fmc_seed_and_independent_reader(self):
        repository = Path(__file__).parents[3] / "fmc-data-model"
        model = compile_repository(repository)
        output = self.root / "generated"
        generate(model, output, "fmc_model")
        module = load_generated(output / "fmc_model", "fmc_model")
        plan = repository / "examples/demo.yaplan"
        self.assertEqual(
            format_document(plan.read_text(encoding="utf-8")),
            plan.read_text(encoding="utf-8"),
        )
        database = self.root / "demo.db"
        files = self.root / "repository"
        configs = []
        for backend, destination in (("sqlite", database), ("files", files)):
            config = self.write(
                backend + ".lclcfg",
                f"backend: '{backend}'\npath: '{destination.name}'\n",
            )
            configs.append(config)
            with asyncio.run(module.FmcData.create(config)) as data:
                report = data.apply(plan)
                self.assertEqual(report.status, "committed")
                undone = data.rollback(report.path)
                self.assertEqual(undone.status, 'committed')
                reader = data.read()
                self.assertEqual(reader.get_fund('FUND00000001').status, Status.NO_ENTRY)
                reader.close()
                self.assertEqual(data.rollback(undone.path).status, 'committed')
        with self.assertRaises(FileExistsError):
            asyncio.run(module.FmcData.create(configs[1]))
        isolated = """import sys, asyncio
from pathlib import Path
from datetime import date
from decimal import Decimal
sys.path.insert(0, sys.argv[1])
import fmc_model as m
assert not any(n.startswith(('yambl_compiler', 'jsonschema')) for n in sys.modules)
for config in sys.argv[2:]:
    data = asyncio.run(m.FmcData.open(Path(config)))
    reader = data.read()
    assert reader.validate().value.valid
    bond = reader.get_security('BOND00000001').value
    assert bond.price.value
    assert bond.issue_date == m.Success(date(2025,1,1))
    assert bond.price_fallback_days == m.Success(30,'model_default')
    req = m.FundIndustryMarketValueRequest
    results = reader.read_fund_industry_market_value_many([
        req(id_value='FUND00000001', as_of_date=date(2026,1,6)),
        req(id_value='FUND00000001', as_of_date=date(2026,1,6), industry_date=date(2026,1,5)),
        req(id_value='FUND00000001', as_of_date=date(2026,1,2), best_effort=True),
    ])
    assert isinstance(results, m.Success), results
    assert dict(results.value[0].value) == {'Financials': Decimal('10451.5'), 'Other': Decimal('38'), 'Technology': Decimal('100')}, results
    assert dict(results.value[1].value) == {'Industrials': Decimal('10451.5'), 'Other': Decimal('38'), 'Technology': Decimal('100')}, results
    assert isinstance(results.value[2],m.Partial), results
    assert results.value[2].value == {'Industrials':Decimal('10175.625')}, results
    reader.close()
    data.close()
    assert bond.read_price(date(2026,1,2),price_source=m.PriceSourcesCode('Primary')) == m.Success(Decimal('101.25'))
print('independent FMC reads and validation passed')
"""
        examples = re.findall(
            r"```python\n(.*?)```",
            (repository / "README.md").read_text(encoding="utf-8"),
            re.S,
        )
        self.assertEqual(len(examples), 2)
        for example in examples:
            for backend, config in zip(("sqlite", "files"), configs, strict=True):
                example = example.replace(
                    f"../fmc-data-model/examples/{backend}.lclcfg", config.as_posix()
                )
            isolated += "\nexec(" + repr(example) + ")\n"
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                isolated,
                str(output),
                *(str(config) for config in configs),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("independent FMC reads and validation passed", completed.stdout)
        self.assertIn("101.25", completed.stdout)
        self.assertIn("Financials", completed.stdout)
        before = database.read_bytes()
        with self.assertRaises(FileExistsError):
            asyncio.run(module.FmcData.create(configs[0]))
        self.assertEqual(database.read_bytes(), before)

        module = load_generated(output / "fmc_model", "fmc_model")
        original = Path.read_text

        def independent(path, *args, **kwargs):
            if path.suffix == ".yambl" or path.name == "result-constants.json":
                raise AssertionError("reader must use bundled IR only")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", independent):
            data = asyncio.run(module.FmcData.open(configs[0]))
            self.addCleanup(data.close)
            reader = data.read()
            self.addCleanup(reader.close)
            bond = reader.get_security("BOND00000001").value
            self.assertIsInstance(bond, module.Bond)
            self.assertEqual(bond.read_issue_date(), Success(date(2025, 1, 1)))
            self.assertEqual(bond.read_issuer_name(), Success("Example issuer"))
            self.assertEqual(
                bond.read_price(date(2026, 1, 2), price_source=module.PriceSourcesCode("Primary")),
                Success(Decimal("101.25")),
            )
            self.assertEqual(bond.read_price_fallback_days(), Success(30, "model_default"))
            self.assertEqual(
                reader.get_security("SECU00000001").value.read_price_fallback_days(),
                Success(5),
            )
            self.assertEqual(
                reader.get_security("SECU00000001").value.read_coupon().status,
                Status.NOT_DEFINED,
            )
            fund = reader.get_fund("FUND00000001").value
            self.assertEqual(
                fund.read_net_asset(date(2026, 1, 2)),
                Success(Decimal("12345678901234567890.125")),
            )
            self.assertEqual(fund.read_holdings(date(2026, 1, 3)), Success(frozenset()))
            self.assertEqual(fund.read_full_name().status, Status.EMPTY)
            ref = reader.get_holding_entry(2).value.read_security().value
            self.assertEqual(reader.resolve(ref).status, Status.ENTITY_NOT_FOUND)
            events = bond.read_notional_value_range(date(2026, 1, 1), date(2026, 1, 8)).value
            self.assertEqual([day.day for day, _ in events], [1, 3, 5, 7])
            self.assertEqual(events[2][1].status, Status.NO_ENTRY)
            reader.close()
        self.assertEqual(database.read_bytes(), before)

    def test_corrupt_physical_data_is_not_recovered_to_other(self):
        repository = Path(__file__).parents[3] / "fmc-data-model"
        output = self.root / "generated"
        generate(compile_repository(repository), output, "fmc_model")
        module = load_generated(output / "fmc_model", "fmc_model")
        config = self.write("data.lclcfg", "backend: 'sqlite'\npath: 'data.db'\n")
        with asyncio.run(module.FmcData.create(config)) as data:
            data.apply(repository / "examples/demo.yaplan")
            for sql in (
                "UPDATE tb_core_security_price SET price='broken' WHERE id='BOND00000002' AND dim_price_source='Primary'",
                "UPDATE tb_core_security_price SET price='50' WHERE id='BOND00000002' AND dim_price_source='Primary'; UPDATE tb_core_issuer_industry SET valid_to='2026-01-07' WHERE id='ISSR00000001' AND valid_to='2026-01-06'",
            ):
                with closing(sqlite3.connect(self.root / "data.db")) as connection, connection:
                    connection.executescript(sql)
                reader = data.read()
                try:
                    result = reader.get_fund("FUND00000001").value.read_industry_market_value(
                        as_of_date=date(2026, 1, 6), best_effort=True
                    )
                    self.assertEqual(result.status, Status.ERROR)
                finally:
                    reader.close()
