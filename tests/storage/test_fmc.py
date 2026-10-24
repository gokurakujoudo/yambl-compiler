"""Exercise the actual FMC example without consulting model sources at read time."""

import importlib.util
import runpy
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success


class FmcTests(FilesystemCase):
    def test_fmc_seed_and_independent_reader(self):
        repository = Path(__file__).parents[3] / 'fmc-data-model'
        model = compile_repository(repository)
        output = self.root / 'generated'
        generate(model, output, 'fmc_model')
        initialize = runpy.run_path(str(repository / 'examples/create_sqlite_demo.py'))['create']
        database = self.root / 'demo.db'
        initialize(output, database)
        before = database.read_bytes()
        with self.assertRaises(FileExistsError):
            initialize(output, database)
        self.assertEqual(database.read_bytes(), before)
        spec = importlib.util.spec_from_file_location('fmc_model', output / 'fmc_model/__init__.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = Path.read_text

        def independent(path, *args, **kwargs):
            if path.suffix == '.yambl' or path.name == 'result-constants.json':
                raise AssertionError('reader must use bundled IR only')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', independent):
            reader = module.FmcReader.open(database).value
            self.addCleanup(reader.close)
            bond = reader.get_security('BOND00000001').value
            self.assertIsInstance(bond, module.Bond)
            self.assertEqual(
                bond.read_price(date(2026, 1, 2), source=module.PriceSourcesCode('Primary')),
                Success(Decimal('101.25')),
            )
            self.assertEqual(bond.read_price_fallback_days(), Success(20, 'model_default'))
            self.assertEqual(
                reader.get_security('SECU00000001').value.read_coupon().status, Status.NOT_DEFINED
            )
            fund = reader.get_fund('FUND00000001').value
            self.assertEqual(
                fund.read_net_asset(date(2026, 1, 2)), Success(Decimal('12345678901234567890.125'))
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
