"""Exercise short-circuiting all through generated calculation packages."""

import unittest
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success


class AllTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    async def test_all_short_circuits_and_propagates_errors_on_both_backends(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write(
            'model.yambl',
            """entity A {
    id id: string
    computed attr empty: bool { return [1].filter(x -> false).all(x -> false) }
    computed attr good: bool { return [1, 2].all(x -> x > 0) }
    computed attr short: bool { return [1, 2].all(x -> if x == 1 then false else ERROR) }
    computed attr error: bool { return [1, 2].all(x -> if x == 1 then ERROR else false) }
    computed attr recovered: bool { return [1].all(x -> ERROR.recover(case ERROR -> true)) }
}
""",
        )
        output = self.root / 'generated'
        generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        plan = Plan('all', None, (Group('g', (Operation('insert', 'A', ('id',), (('a',),)),)),))
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await module.SampleData.create(config) as store:
                    self.assertEqual(store.apply(plan).status, 'committed')
                    with closing(store.read()) as reader:
                        entity = reader.get_a('a').value
                        self.assertEqual(entity.read_empty(), Success(True, 'computation'))
                        self.assertEqual(entity.read_good(), Success(True, 'computation'))
                        self.assertEqual(entity.read_short(), Success(False, 'computation'))
                        self.assertEqual(entity.read_error().status, Status.ERROR)
                        self.assertEqual(entity.read_recovered(), Success(True, 'computation'))

    def test_all_requires_a_boolean_predicate(self):
        from tests.support import document
        from yambl_compiler.languages.yambl.semantics.validation import check

        source = 'entity A { computed attr x: bool { return [1].all(x -> x) } }'
        self.assertTrue(
            any(
                'all predicate requires bool' in issue.message
                for issue in check([document(source)])
            )
        )
