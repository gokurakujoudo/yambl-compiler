"""Exact arithmetic, partial outcomes and batched effective configuration."""

import json
from dataclasses import asdict
from decimal import Decimal, localcontext
from unittest import TestCase
from unittest.mock import patch

from tests.storage.test_dual import DualTests
from yambl_compiler.storage.arithmetic import aggregate, failure, lift, numeric
from yambl_compiler.storage.batch import FieldRequest
from yambl_compiler.storage.configuration import Configuration
from yambl_compiler.storage.results import (
    CalculationIssue,
    EntityRef,
    NonNormalResult,
    Partial,
    Status,
    Success,
)


class ArithmeticTests(TestCase):
    def test_precision_is_independent_of_ambient_context(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(numeric("*", Decimal("12.3"), Decimal("4.56")), Decimal("56.088"))
            self.assertEqual(
                numeric("+", Decimal("12345678901234567890"), Decimal("0.00001")),
                Decimal("12345678901234567890.00001"),
            )
            self.assertEqual(numeric("-", Decimal("1"), Decimal("0.0001")), Decimal("0.9999"))
            self.assertEqual(
                aggregate([Success(Decimal("1.234")), Success(Decimal("2.345"))], False),
                Success(Decimal("3.579"), "computation"),
            )
        self.assertEqual(numeric("*", 10**30, 10**30), 10**60)
        self.assertEqual(numeric("+", 2.0, Decimal("0.5")), 2.5)
        for operator, left, right in [
            ("%", 1, 2),
            ("+", True, 1),
            ("+", float("inf"), 1),
            ("+", Decimal("NaN"), 1),
        ]:
            with self.subTest(operator=operator, left=left):
                with self.assertRaises(ValueError):
                    numeric(operator, left, right)

    def test_division_returns_float_and_floor_division_returns_integer(self):
        for left, right, expected in [
            (1, 3, 1 / 3),
            (10**1000, 10**999, 10.0),
            (Decimal('1.25'), Decimal('2.5'), 0.5),
        ]:
            result = numeric('/', left, right)
            self.assertIs(type(result), float)
            self.assertEqual(result, expected)
        for left, right, expected in [
            (-7, 3, -3),
            (7, -3, -3),
            (-7, -3, 2),
            (Decimal('-1.25'), Decimal('0.5'), -3),
            (7.0, 3.0, 2),
        ]:
            result = numeric('//', left, right)
            self.assertIs(type(result), int)
            self.assertEqual(result, expected)
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(
                numeric('//', Decimal('12345678901234567890'), Decimal('0.01')),
                1234567890123456789000,
            )
        for op, left, right in [
            ('/', 1, 0),
            ('//', 0, 0),
            ('/', 10**1000, 1),
            ('/', float('inf'), 1),
            ('//', Decimal('NaN'), 1),
        ]:
            result = lift(lambda a, b: numeric(op, a, b), [Success(left), Success(right)])
            self.assertEqual(result.status, Status.ERROR)

    def test_strict_best_effort_and_partial_lifting(self):
        missing = NonNormalResult(Status.NO_ENTRY, "price absent")
        empty = NonNormalResult(Status.EMPTY)
        self.assertEqual(aggregate([missing, missing], True).status, Status.NO_ENTRY)
        self.assertEqual(aggregate([missing, empty], True).status, Status.ERROR)
        self.assertEqual(aggregate([], True), Success(0, "computation"))
        self.assertEqual(aggregate([Success(2), missing], False).status, Status.NO_ENTRY)
        partial = aggregate([Success(2), missing], True)
        self.assertIsInstance(partial, Partial)
        self.assertEqual(partial.value, 2)
        self.assertEqual(partial.issues[0].status, Status.NO_ENTRY)
        doubled = lift(lambda x: x * 2, [partial])
        self.assertEqual(doubled.value, 4)
        self.assertEqual(doubled.issues, partial.issues)
        self.assertEqual(
            aggregate([Success(1), NonNormalResult(Status.ERROR)], True).status,
            Status.ERROR,
        )
        self.assertEqual(lift(lambda x: x, [missing]).status, Status.NO_ENTRY)
        self.assertEqual(lift(lambda x: int(x), [Success("bad")]).status, Status.ERROR)
        self.assertEqual(aggregate([Success("bad")], False).status, Status.ERROR)
        self.assertEqual(failure([missing]).message, "price absent")
        for construction in (
            lambda: NonNormalResult(Status.PARTIAL),
            lambda: Partial(1, ()),
            lambda: Partial(1, (CalculationIssue(Status.ERROR),)),
        ):
            with self.assertRaises(ValueError):
                construction()


class ConfigurationTests(DualTests):
    def test_typed_collection_assignments_and_invalid_group_records(self):
        self.group("peers", 1, {"A.peers": ["10", "11"]})
        reader = self.open()
        result = reader.get_a("a").value.read_peers()
        self.assertEqual(result.origin, "config_group")
        self.assertEqual({r.id for r in result.value}, {10, 11})
        config = reader._configuration
        type_ = next(f.type for f in self.model.fields if f.identity == "A.peers")
        for payload in ["not a list", ["10", "10"]]:
            with self.assertRaises(ValueError):
                config.value(type_, payload)

        types = {f.identity: f.type for f in self.model.fields}

        def entry(identity="A.limit", payload="2", type_=None):
            return {
                "config_group_id": "g",
                "field": identity,
                "value": json.dumps({"type": asdict(type_ or types[identity]), "value": payload}),
            }

        header = {
            "config_group_id": "g",
            "target_entity_type": "A",
            "priority": "1",
            **{k + "__state": 0 for k in ("target_entity_type", "priority", "when", "then_set")},
        }
        bad_cases = [
            ([header], [], [{**entry(), "config_group_id": "unknown"}]),
            ([header], [], [{**entry(), "field": "missing"}]),
            ([header], [], [{**entry(), "value": "{}"}]),
            ([header], [], [entry(type_=types["A.text"])]),
            ([header], [], [entry("A.text", "x")]),
            ([header], [], [entry(), entry()]),
            ([{**header, "priority__state": 2}], [], []),
            ([{**header, "priority": 1}], [], []),
            ([{**header, "priority": "01"}], [], []),
            ([{**header, "target_entity_type": "missing"}], [], []),
            ([{**header, "target_entity_type": "Other"}], [], [entry()]),
            ([header, header], [], []),
        ]
        for headers, conditions, assignments in bad_cases:
            with (
                self.subTest(headers=headers, assignments=assignments),
                patch.object(
                    reader._backend,
                    "rows_many",
                    side_effect=[[headers], [conditions], [assignments]],
                ),
            ):
                with self.assertRaises(ValueError):
                    Configuration(reader._batch).load()

    def test_group_nonmatches_missing_assignments_and_actual_exact_constraint(self):
        self.group("empty_condition", 4, {"A.limit": "7"}, {"A.text": "never"})
        self.group("other_target", 5, {"A.limit": "8"}, target="C")
        self.group("different_field", 6, {"A.enabled": 1})
        reader = self.open()
        result = reader.get_a("a").value.read_limit()
        self.assertEqual(result, Success(20, "model_default"))
        self.assertEqual(
            reader._configuration.read([FieldRequest(EntityRef("A", "a"), "missing")])[0].status,
            Status.NOT_DEFINED,
        )
        reader._configuration.load()
        with patch.object(
            reader._configuration,
            "value",
            side_effect=ValueError("actual subtype constraint"),
        ):
            result = reader._configuration.read([FieldRequest(EntityRef("C", "c"), "A.enabled")])[0]
            self.assertEqual(result.status, Status.ERROR)

    def group(self, name, priority, assignments, conditions=None, target="A"):
        self.writer.execute(
            "INSERT INTO tb_config_group (config_group_id, target_entity_type__state, target_entity_type, priority__state, priority, when__state, then_set__state) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, 0, target, 0, str(priority), 0, 0),
        )
        fields = {f.identity: f.type for f in self.model.fields}
        for role, entries in [("when", conditions or {}), ("then_set", assignments)]:
            for identity, value in entries.items():
                envelope = json.dumps({"type": asdict(fields[identity]), "value": value})
                self.writer.execute(
                    "INSERT INTO tb_config_group_"
                    + role
                    + " (config_group_id, field, value) VALUES (?, ?, ?)",
                    (name, identity, envelope),
                )
        self.writer.commit()

    def test_configuration_priority_default_and_matching(self):
        self.put("A.text", "selected", id="c")
        self.put("A.limit", "5", id="c")
        self.group("low", 10, {"A.limit": "7"}, {"A.text": "selected"})
        self.group("high", 10**30, {"A.limit": "3"}, {"A.text": "selected"})
        path = self.files()
        for reader in (self.open(), self.module.SampleReader.open_files(path).value):
            self.addCleanup(reader.close)
            c = reader.get_c("c").value
            self.assertEqual(c.read_limit(), Success(3, "config_group", ("high",)))
            self.assertEqual(reader.get_a("a").value.read_limit(), Success(20, "model_default"))
            reader.close()
            self.assertEqual(c.read_limit(), Success(3, "config_group", ("high",)))

    def test_configuration_matches_hidden_type_fields_on_both_backends(self):
        self.group('type', 10, {'A.limit': '9'}, {'A.entity_type': 'C', 'A.root_entity_type': 'A'})
        path = self.files()
        for reader in (self.open(), self.module.SampleReader.open_files(path).value):
            self.addCleanup(reader.close)
            self.assertEqual(reader.get_a('a').value.read_limit(), Success(20, 'model_default'))
            self.assertEqual(
                reader.get_a('c').value.read_limit(), Success(9, 'config_group', ('type',))
            )

    def test_same_priority_merge_and_conflict(self):
        self.group("one", 5, {"A.limit": "2"})
        self.group("two", 5, {"A.limit": "2"})
        first = self.open()
        self.assertEqual(
            first.get_a("a").value.read_limit(),
            Success(2, "config_group", ("one", "two")),
        )
        first.close()
        self.group("three", 5, {"A.limit": "3"})
        self.assertEqual(self.open().get_a("a").value.read_limit().status, Status.ERROR)

    def test_when_error_is_not_a_nonmatch(self):
        self.put("A.count", "01")
        self.group("bad_condition", 1, {"A.limit": "3"}, {"A.count": "1"})
        result = self.open().get_a("a").value.read_limit()
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("noncanonical integer", result.message)

    def test_get_many_uses_one_backend_batch(self):
        reader = self.open()
        with patch.object(reader._backend, "rows", side_effect=AssertionError("scalar lookup")):
            result = reader.get_a_many(["a", "c", "missing", "a"])
        self.assertEqual(
            [r.value.id if isinstance(r, Success) else r.status for r in result.value],
            ["a", "c", Status.NO_ENTRY, "a"],
        )
