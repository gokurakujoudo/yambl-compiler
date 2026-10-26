"""Exercise generated transaction packages without compiler imports."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

from tests.transactions.test_binding import CatalogCase
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.model import Group, Operation, Plan
from yambl_compiler.transactions.writing import dumps

SCRIPT = '''
import asyncio
import importlib.abc
from pathlib import Path
import runpy
import sys

class RejectCompiler(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'yambl_compiler' or fullname.startswith('yambl_compiler.'):
            raise ImportError('compiler must not be imported')
sys.meta_path.insert(0, RejectCompiler())
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from standalone_model import StandaloneData, Status, Success

async def exercise():
    for config in (root / 'sqlite.lclcfg', root / 'files.lclcfg'):
        with await StandaloneData.create(config) as data:
            empty = data.read()
            applied = data.apply(root / 'demo.yaplan')
            assert applied.status == 'committed'
            populated = data.read()
            assert populated.get_a('a').value.text == Success('original')
            assert empty.get_a('a').status == Status.NO_ENTRY
            assert populated.validate().value.valid
            reverted = data.rollback(applied.path)
            assert reverted.status == 'committed'
            assert populated.get_a('a').value.text == Success('original')
            restored = data.rollback(reverted.path)
            assert restored.status == 'committed'
            assert data.export(applied.id).read_bytes() == applied.path.read_bytes()
            empty.close()
            populated.close()
        with await StandaloneData.open(config) as data:
            reader = data.read()
            assert reader.get_a('a').value.text == Success('original')
            reader.close()
    assert not any(n.startswith('yambl_compiler') for n in sys.modules)
    print('both backends: create/apply/read/validate/rollback/redo/reopen passed')
asyncio.run(exercise())
sys.argv = ['standalone_model', 'check-data', '-o', 'config', str(root / 'files.lclcfg')]
runpy.run_module('standalone_model', run_name='__main__')
'''


class GeneratedTransactionTests(CatalogCase):
    def test_standalone_generated_interface_and_dependency_declaration(self):
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'standalone_model')
        project = tomllib.loads((output / 'pyproject.toml').read_text(encoding='utf-8'))
        compiler = tomllib.loads(
            (Path(__file__).parents[2] / 'pyproject.toml').read_text(encoding='utf-8')
        )
        expected = [d for d in compiler['project']['dependencies'] if d.startswith('lclang')]
        self.assertEqual(project['project']['dependencies'], expected)
        self.assertEqual(project['project']['name'], 'standalone-model')
        self.assertEqual(
            (output / 'standalone_model/schema.sql').read_bytes(),
            (output / 'schema.sql').read_bytes(),
        )
        operation = Operation('insert', 'A', ('key', 'text'), (('a', 'original'),))
        plan = Plan('demo', self.catalog.layout.model.model_digest, (Group('all', (operation,)),))
        (output / 'demo.yaplan').write_text(dumps(plan), encoding='utf-8')
        for backend in ('sqlite', 'files'):
            (output / (backend + '.lclcfg')).write_text(
                f"backend: '{backend}'\npath: '{backend}.data'\n", encoding='utf-8'
            )
        child = subprocess.run(
            [sys.executable, '-I', '-c', SCRIPT, str(output)],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertIn('"valid": true', child.stdout)
        self.assertIn(
            'both backends: create/apply/read/validate/rollback/redo/reopen passed', child.stdout
        )
        for backend in ('sqlite', 'files'):
            self.assertTrue((output / (backend + '.data')).exists())
        self.assertEqual(
            json.loads((output / 'files.data/repository.json').read_text())['format_revision'], 2
        )

    def test_missing_or_duplicate_runtime_dependency_is_a_preflight_failure(self):
        for dependencies in (None, [], ['lclang==1', 'lclang==2']):
            with self.subTest(dependencies=dependencies):
                output = self.root / 'invalid'
                with patch('yambl_compiler.storage.generation.requires', return_value=dependencies):
                    with self.assertRaisesRegex(ValueError, 'one lclang'):
                        generate(self.catalog.layout.model, output, 'standalone_model')
                self.assertFalse(output.exists())
