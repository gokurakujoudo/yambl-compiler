"""Check transaction boundaries for external dictionaries and corrupt mappings."""

from copy import deepcopy

from tests.transactions.test_binding import CatalogCase
from yambl_compiler.languages.shared.model import Condition, ObjectValue
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.languages.yatran.model import Image
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution
from yambl_compiler.transactions.validation import PlanValidator


class DictionaryBoundaryTests(CatalogCase):
    def setUp(self):
        super().setUp()
        self.data = Data(
            self.catalog.layout,
            {
                table.name: [
                    dict(zip((column.name for column in table.columns), row, strict=True))
                    for row in table.rows
                ]
                for table in self.catalog.layout.tables
            },
        )

    def test_external_dictionary_replacements_do_not_claim_model_contract(self):
        validator = PlanValidator(self.catalog.layout)
        for identity in ('ExternalLive', 'A'):
            scope = (Condition('decodes_id', identity),)
            operation = Operation(
                'replace', 'builtins.Decodes.entries', ('value',), (((),),), scope
            )
            validator.validate(Plan('external', None, (Group('change', (operation,)),)))
            self.assertEqual(validator.codes, {})

    def test_external_dictionary_expectations_read_snapshot_without_writing(self):
        identity = 'ExternalLive'
        entity = self.catalog.entity('builtins.Decodes')
        scope = (Condition('decodes_id', identity),)
        self.data.insert(entity, identity)
        self.data.put(
            'builtins.Decodes.entries',
            scope,
            Image('builtins.Decodes.entries', ('value',), (((),),)),
        )
        operation = Operation(
            'replace', 'builtins.Decodes.entries', ('value',), (((),),), scope, expect='one'
        )
        plan = Plan('external', None, (Group('change', (operation,)),))
        before = deepcopy(self.data.tables)
        Execution(self.data).validate_expectations(plan)
        self.assertEqual(self.data.tables, before)
        image = self.data.image('builtins.Decodes.entries', scope)
        self.assertEqual(image.rows, ((identity, ()),))

    def test_unknown_config_mapping_field_is_rejected_while_reading_image(self):
        entity = self.catalog.entity('builtins.ConfigGroup')
        scope = (Condition('config_group_id', 'g'),)
        self.data.insert(entity, 'g')
        self.data.put(
            'builtins.ConfigGroup.when',
            scope,
            Image('builtins.ConfigGroup.when', ('value',), ((ObjectValue(()),),)),
        )
        self.data.add(
            'tb_config_group_when',
            {
                'config_group_id': 'g',
                'field': 'missing.field',
                'value': '{"type":{},"value":"x"}',
            },
        )
        before = deepcopy(self.data.tables)
        with self.assertRaisesRegex(ValueError, 'unknown configuration mapping field'):
            self.data.image('builtins.ConfigGroup.when', scope)
        self.assertEqual(self.data.tables, before)
