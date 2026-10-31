"""Exercise dictionary reference changes through real transactions on both backends."""

import unittest
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.writing import dumps

SOURCE = '''namespace core
decodes Sources { - 100 Known: 'Known' }
decodes OtherSources { - 100 Known: 'Known' }
entity Item {
    id key: string
    static attr source: code<Sources>
    config attr default_source: code<Sources>
    dimension vendor_source { value vendor: code<Sources> }
    @dimension(vendor_source) @temporal(day: date) snapshot attr price: decimal
    @temporal(day: date) snapshot attr observed: code<Sources>
    @temporal(day: date) scd attr rating: code<Sources>
}
entity Root { id root_id: string }
entity Left inherits Root { @overload static attr category: code<Sources> }
entity Right inherits Root { @overload static attr category: code<OtherSources> }
'''


class DictionaryTransactionTests(FilesystemCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        self.model = compile_repository(self.root)
        output = self.root / 'generated'
        generate(self.model, output, 'dictionary_model')
        self.api = load_generated(output / 'dictionary_model', 'dictionary_model')
        self.original = (self.entry('Known', 'Known', 100),)
        self.extended = (*self.original, self.entry('Extra', 'Extra', 200))
        self.dictionary_scope = (Condition('decodes_id', 'core.Sources'),)
        self.item_scope = (Condition('key', 'item'),)
        self.group_scope = (Condition('config_group_id', 'group'),)

    @staticmethod
    def entry(code, decode, ordering):
        return ObjectValue(
            ((Symbol('code'), code), (Symbol('decode'), decode), (Symbol('ordering'), ordering))
        )

    def dictionary(self, entries):
        return Operation(
            'replace', 'builtins.Decodes.entries', ('value',), ((entries,),), self.dictionary_scope
        )

    def plan(self, name, *operations):
        value = Plan(
            name.replace('-', '_'), self.model.model_digest, (Group('change', tuple(operations)),)
        )
        return self.write(name + '.yaplan', dumps(value))

    def group(self, target, conditions, assignments):
        return Operation(
            'insert',
            'builtins.ConfigGroup',
            ('config_group_id', 'target_entity_type', 'priority', 'when', 'then_set'),
            (('group', Symbol(target), 1, ObjectValue(conditions), ObjectValue(assignments)),),
        )

    async def test_delete_and_rollback_protect_all_reference_locations(self):
        scenarios = (
            (
                'dimension',
                (
                    Operation(
                        'replace',
                        'core.Item.price',
                        ('vendor', 'day', 'value'),
                        (('Extra', '2025-01-01', 5),),
                        self.item_scope,
                    ),
                ),
                (Operation('delete', 'core.Item.price', (), (), self.item_scope),),
            ),
            (
                'snapshot',
                (
                    Operation(
                        'replace',
                        'core.Item.observed',
                        ('day', 'value'),
                        (('2025-01-01', 'Extra'),),
                        self.item_scope,
                    ),
                ),
                (Operation('delete', 'core.Item.observed', (), (), self.item_scope),),
            ),
            (
                'scd',
                (
                    Operation(
                        'replace',
                        'core.Item.rating',
                        ('valid_from', 'valid_to', 'value'),
                        (('2024-01-01', '2025-01-01', 'Extra'),),
                        self.item_scope,
                    ),
                ),
                (Operation('delete', 'core.Item.rating', (), (), self.item_scope),),
            ),
            (
                'config_when',
                (self.group('core.Item', ((Symbol('core.Item.source'), 'Extra'),), ()),),
                (
                    Operation(
                        'replace',
                        'builtins.ConfigGroup.when',
                        ('value',),
                        ((ObjectValue(()),),),
                        self.group_scope,
                    ),
                ),
            ),
            (
                'config_then',
                (self.group('core.Item', (), ((Symbol('core.Item.default_source'), 'Extra'),)),),
                (
                    Operation(
                        'replace',
                        'builtins.ConfigGroup.then_set',
                        ('value',),
                        ((ObjectValue(()),),),
                        self.group_scope,
                    ),
                ),
            ),
            (
                'overload',
                (self.group('core.Root', ((Symbol('core.Root.category'), 'Extra'),), ()),),
                (
                    Operation(
                        'replace',
                        'builtins.ConfigGroup.when',
                        ('value',),
                        ((ObjectValue(()),),),
                        self.group_scope,
                    ),
                ),
            ),
        )
        for backend in ('sqlite', 'files'):
            for name, references, clear in scenarios:
                with self.subTest(backend=backend, reference=name):
                    config = self.write(
                        f'{backend}-{name}.lclcfg',
                        f"backend: '{backend}'\npath: '{backend}-{name}.data'\n",
                    )
                    with await self.api.DictionaryData.create(config) as store:
                        store.apply(
                            self.plan(
                                'initial', Operation('insert', 'core.Item', ('key',), (('item',),))
                            )
                        )
                        extended = store.apply(self.plan('extend', self.dictionary(self.extended)))
                        store.apply(self.plan('reference', *references))
                        before = store.snapshot().tables
                        exports = set(store.transaction_directory().glob('*.yatran'))
                        with self.assertRaisesRegex(ValueError, 'unknown code for core.Sources'):
                            store.apply(self.plan('remove-invalid', self.dictionary(self.original)))
                        self.assertEqual(store.snapshot().tables, before)
                        self.assertEqual(
                            set(store.transaction_directory().glob('*.yatran')), exports
                        )
                        with self.assertRaisesRegex(ValueError, 'unknown code for core.Sources'):
                            store.rollback(extended.path)
                        self.assertEqual(store.snapshot().tables, before)
                        self.assertEqual(
                            set(store.transaction_directory().glob('*.yatran')), exports
                        )
                        removed = store.apply(
                            self.plan(
                                'remove-with-references', self.dictionary(self.original), *clear
                            )
                        )
                        after = store.snapshot().tables
                        restored = store.rollback(removed.path)
                        self.assertEqual(store.snapshot().tables, before)
                        store.rollback(restored.path)
                        self.assertEqual(store.snapshot().tables, after)

    async def test_reader_dictionary_snapshots_survive_later_commit(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                with await self.api.DictionaryData.create(config) as store:
                    store.apply(
                        self.plan(
                            'initial',
                            Operation(
                                'insert', 'core.Item', ('key', 'source'), (('item', 'Known'),)
                            ),
                        )
                    )
                    old = store.read()
                    self.addCleanup(old.close)
                    old_item = old.get_item('item').value
                    self.assertEqual(str(old_item.read_source().value), 'Known')
                    store.apply(
                        self.plan(
                            'change',
                            self.dictionary(self.extended),
                            Operation(
                                'update', 'core.Item', ('key', 'source'), (('item', 'Extra'),)
                            ),
                        )
                    )
                    with closing(store.read()) as new:
                        self.assertEqual(
                            str(new.get_item('item').value.read_source().value), 'Extra'
                        )
                    self.assertEqual(str(old_item.read_source().value), 'Known')
                    type_ = next(
                        f.type for f in self.model.fields if f.identity == 'core.Item.source'
                    )
                    with self.assertRaisesRegex(ValueError, 'unknown code'):
                        old._decode(type_, 'Extra')

    async def test_overload_other_dictionary_does_not_block_unrelated_removal(self):
        other_scope = (Condition('decodes_id', 'core.OtherSources'),)
        other_extension = Operation(
            'replace', 'builtins.Decodes.entries', ('value',), ((self.extended,),), other_scope
        )
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                config = self.write(
                    f'{backend}-other.lclcfg',
                    f"backend: '{backend}'\npath: '{backend}-other.data'\n",
                )
                with await self.api.DictionaryData.create(config) as store:
                    store.apply(
                        self.plan('extend', self.dictionary(self.extended), other_extension)
                    )
                    store.apply(
                        self.plan(
                            'group',
                            self.group(
                                'core.Right', ((Symbol('core.Root.category'), 'Extra'),), ()
                            ),
                        )
                    )
                    result = store.apply(self.plan('remove', self.dictionary(self.original)))
                    self.assertEqual(result.status, 'committed')
                    self.assertEqual(len(store.snapshot().tables['tb_config_group_when']), 1)
