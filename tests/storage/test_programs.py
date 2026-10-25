"""Run generated calculations against real SQLite and JSONL repositories."""

import ast
import io
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from docutils import nodes
from docutils.core import publish_doctree

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.backends import Selection, repository_contents
from yambl_compiler.storage.batch import FieldRequest
from yambl_compiler.storage.computing import UNSET, Frame, Groups
from yambl_compiler.storage.generation import generate, python_package
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.programs import Emitter, programs
from yambl_compiler.storage.records import quote, table_name
from yambl_compiler.storage.results import (
    CalculationIssue,
    EntityRef,
    NonNormalResult,
    Partial,
    Status,
    Success,
)
from yambl_compiler.storage.selection import Available, BestRequest

SOURCE = """
decodes Industries { - 1 Primary: 'Primary' }
entity Issuer {
    id key: string
    dimension industries { value industry_type: code<Industries> }
    @dimension(industries) @temporal(as_of_date: date)
    scd attr industry: string
}
entity Security {
    id key: string
    static attr issuer: Issuer
    config attr days: integer = 3
    dimension prices {
        value source: code<'Sources'>
        hierarchy hierarchy: Hierarchy = 'Local'
        fallback_days = self.days
    }
    @dimension(prices) @temporal(as_of_date: date)
    snapshot attr price: decimal
    computed attr industry: string { return self.issuer.industry::at(industry_type=Industries.Primary) }
}
entity Holding {
    id key: string
    static attr security: Security
    static attr quantity: decimal
    computed attr amount: decimal { return self.quantity * self.security.price::best_available() }
}
entity Bond inherits Security {}
entity Fund {
    id key: string
    scd attr members: collection<Holding>
    @temporal(as_of_date: date) snapshot attr holdings: collection<Holding>
    computed attr amount: mapping<string, decimal> {
        let holdings = self.holdings::at()
        return holdings.group_by(h -> h.security.industry::value(as_of_date=input.industry_date ?? input.as_of_date).recover(case NO_ENTRY -> 'Other')).sum_by(h -> h.amount::value(as_of_date=input.price_date ?? input.as_of_date))
    }
}
extend Security {
    @extra_input_params(day: date = '2026-01-10', 'Requested date')
    computed attr declared_price: decimal { return self.price::best_available(as_of_date=input.day ?? input.unused_day) }
    @extra_input_params(count: integer)
    computed attr input_value: integer { return input.count ?? 7 }
    computed attr identity_text: string { return self.key }
    computed attr safe: integer { return if true and not false then -2 + 3 * 4 else self.days }
    computed attr recovered: integer { return ERROR.recover(case ERROR -> 42) }
    computed attr preserved: integer { return EMPTY.recover(case NO_ENTRY -> 0) }
    computed attr overflow: decimal[3, 1] { return 123.45 }
    computed attr choose: integer { return if false or false then self.days else 8 }
}
extend Fund {
    computed attr filtered: decimal {
        return self.holdings::at().filter(h -> h.quantity >= 3).map(h -> h.quantity).sum_by(q -> q)
    }
    computed attr expanded: decimal {
        return self.holdings::at().flat_map(h -> self.holdings::at()).sum_by(h -> h.quantity)
    }
    computed attr accepted: mapping<string, decimal> {
        return self.amount::value().recover(case PARTIAL as p -> p.value)
    }
}
"""


class ProgramTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        self.write("yambl-root.lclcfg", "prefix: ''\n")
        self.write("main.yambl", SOURCE)
        self.model = compile_repository(self.root)
        self.output = self.root / "generated"
        self.layout = generate(self.model, self.output, "sample_model")
        self.module = load_generated(self.output / "sample_model", "sample_model")
        self.path = self.root / "example.db"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA foreign_keys = ON")
        self.writer.executescript(
            "BEGIN;\n" + (self.output / "schema.sql").read_text(encoding="utf-8") + "\nCOMMIT;"
        )
        for entity, ids in [
            ("Fund", ["f"]),
            ("Holding", ["h1", "h2"]),
            ("Security", ["s"]),
            ("Issuer", ["i"]),
        ]:
            for id in ids:
                self.writer.execute(
                    f"INSERT INTO {quote(table_name(entity))} (id, entity_type) VALUES (?, ?)",
                    (id, entity),
                )
        self.writer.execute(
            "INSERT INTO tb_hierarchy (hierarchy_id, hierarchy_entries__state) VALUES ('Local', 'VALUE')"
        )
        self.writer.executemany(
            "INSERT INTO tb_hierarchy_entries (hierarchy_id, entry_value, ordering) VALUES (?, ?, ?)",
            [("Local", "X", "1"), ("Local", "Y", "2")],
        )
        self.put("Security.issuer", "i", id="s")
        for id, qty in [("h1", "2"), ("h2", "3")]:
            self.put("Holding.security", "s", id=id)
            self.put("Holding.quantity", qty, id=id)
        self.put("Fund.holdings", id="f", date="2026-01-10")
        member = self.layout.fields["Fund.holdings"].members
        self.writer.executemany(
            f"INSERT INTO {quote(member)} (id, date, member_id) VALUES (?, ?, ?)",
            [("f", "2026-01-10", "h1"), ("f", "2026-01-10", "h2")],
        )
        self.put("Security.price", "2.5", id="s", dim_source="X", date="2026-01-09")
        self.put("Security.price", "3", id="s", dim_source="Y", date="2026-01-10")
        self.put(
            "Issuer.industry",
            "Bank",
            id="i",
            dim_industry_type="Primary",
            valid_from="2026-01-01",
            valid_to="2026-01-11",
        )
        self.put(
            "Issuer.industry",
            "Technology",
            id="i",
            dim_industry_type="Primary",
            valid_from="2026-01-11",
            valid_to="2999-12-31",
        )
        self.writer.commit()

    def put(self, identity, value=None, id="", **keys):
        stored = self.layout.fields[identity]
        values = {"id": id, **dict(stored.key_values), **keys, stored.state: "VALUE"}
        if stored.value:
            values[stored.value] = value
        if stored.table == table_name(stored.root):
            values["entity_type"] = stored.root
        columns = ", ".join(quote(k) for k in values)
        self.writer.execute(
            f'INSERT INTO {quote(stored.table)} ({columns}) VALUES ({", ".join("?" for _ in values)}) ON CONFLICT DO UPDATE SET '
            + ", ".join(quote(k) + " = excluded." + quote(k) for k in values),
            tuple(values.values()),
        )

    def readers(self):
        self.writer.row_factory = sqlite3.Row
        tables = {
            t.name: [dict(row) for row in self.writer.execute("SELECT * FROM " + quote(t.name))]
            for t in self.layout.tables
        }
        files = self.root / "files"
        for name, content in repository_contents(self.layout, tables).items():
            file = files / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8", newline="\n")
        for result in (
            self.module.SampleReader.open_sqlite(self.path),
            self.module.SampleReader.open_files(files),
        ):
            self.assertIsInstance(result, Success)
            self.addCleanup(result.value.close)
            yield result.value

    def test_declared_defaults_and_omission_operator_on_both_backends(self):
        for reader in self.readers():
            security = reader.get_security('s').value
            self.assertEqual(security.read_declared_price().value, Decimal('3'))
            self.assertEqual(
                security.read_declared_price(day=date(2026, 1, 9)).value, Decimal('2.5')
            )
            self.assertEqual(security.read_input_value().value, 7)
            self.assertEqual(security.read_input_value(count=0).value, 0)
            self.assertNotIn('unused_day', security.read_declared_price.__annotations__)

    def test_generated_batch_dates_defaults_and_repeated_contributions(self):
        for reader in self.readers():
            request = self.module.FundAmountRequest
            with patch.object(
                reader._backend, "rows", side_effect=AssertionError("N+1 scalar lookup")
            ):
                results = reader.read_fund_amount_many(
                    [
                        request(id_value="f", as_of_date=date(2026, 1, 10)),
                        request(
                            id_value="f",
                            as_of_date=date(2026, 1, 10),
                            price_date=date(2026, 1, 9),
                            industry_date=date(2026, 1, 11),
                        ),
                        request(id_value="f", as_of_date=date(2026, 1, 10)),
                    ]
                )
            self.assertIsInstance(results, Success, results)
            self.assertEqual(
                [dict(r.value) for r in results.value],
                [
                    {"Bank": Decimal("15")},
                    {"Technology": Decimal("12.5")},
                    {"Bank": Decimal("15")},
                ],
            )
            with self.assertRaises(TypeError):
                results.value[0].value["Bank"] = Decimal(0)
            reader.close()
            cached = reader.read_fund_amount_many(
                [request(id_value="f", as_of_date=date(2026, 1, 10))]
            )
            self.assertEqual(dict(cached.value[0].value), {"Bank": Decimal("15")})

    def test_batch_query_count_and_file_index_reuse(self):
        sqlite, files = list(self.readers())
        request = self.module.FundAmountRequest
        repeated = [request(id_value="f", as_of_date=date(2026, 1, 10))] * 64
        with patch.object(sqlite._backend, "rows_many", wraps=sqlite._backend.rows_many) as queries:
            result = sqlite.read_fund_amount_many(repeated)
        self.assertEqual(len(result.value), 64)
        query_count = queries.call_count
        fresh = self.module.SampleReader.open_sqlite(self.path).value
        self.addCleanup(fresh.close)
        with patch.object(fresh._backend, "rows_many", wraps=fresh._backend.rows_many) as queries:
            fresh.read_fund_amount_many(repeated[:1])
        self.assertEqual(queries.call_count, query_count)
        original = Path.read_bytes
        loads = []

        def tracked(path):
            loads.append(path)
            return original(path)

        with patch.object(Path, "read_bytes", tracked):
            result = files.read_fund_amount_many(repeated)
            indexes = dict(files._backend.time_indexes)
            other = files.read_fund_amount_many(
                [request(id_value="f", as_of_date=date(2026, 1, 10), price_date=date(2026, 1, 9))]
            )
        self.assertEqual(result.value[0].value, {"Bank": Decimal(15)})
        self.assertEqual(other.value[0].value, {"Bank": Decimal("12.5")})
        self.assertEqual(len(loads), len(set(loads)))
        self.assertTrue(indexes)
        for key, index in indexes.items():
            self.assertIs(files._backend.time_indexes[key], index)

    def test_computed_request_and_method_docs_are_valid_rst(self):
        source = (self.output / "sample_model/__init__.py").read_text(encoding="utf-8")
        self.assertIn("Sources: main.yambl:", source)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                stream = io.StringIO()
                tree = publish_doctree(
                    ast.get_docstring(node),
                    settings_overrides={
                        "report_level": 2,
                        "halt_level": 5,
                        "warning_stream": stream,
                    },
                )
                self.assertFalse(list(tree.findall(nodes.system_message)), stream.getvalue())

    def test_partial_and_recovery(self):
        self.put("Holding.security", "missing", id="h2")
        self.writer.commit()
        for reader in self.readers():
            request = self.module.FundAmountRequest
            strict = reader.read_fund_amount_many(
                [request(id_value="f", as_of_date=date(2026, 1, 10))]
            )
            self.assertIsInstance(strict, Success, strict)
            self.assertEqual(strict.value[0].status, Status.ENTITY_NOT_FOUND)
            loose = reader.read_fund_amount_many(
                [request(id_value="f", as_of_date=date(2026, 1, 10), best_effort=True)]
            )
            self.assertIsInstance(loose.value[0], Partial)
            self.assertEqual(dict(loose.value[0].value), {"Bank": Decimal(6)})
            self.assertTrue(loose.value[0].issues)
            accepted = reader.calculate_many(
                "Fund",
                "Fund.accepted",
                [("f", {"as_of_date": date(2026, 1, 10)}, True)],
            )
            self.assertIsInstance(accepted.value[0], Success)
            self.assertEqual(dict(accepted.value[0].value), {"Bank": Decimal(6)})

    def test_scalar_branches_collection_methods_and_bad_requests(self):
        for reader in self.readers():
            for name, expected in [
                ("identity_text", "s"),
                ("safe", 10),
                ("recovered", 42),
                ("choose", 8),
            ]:
                result = reader.calculate_many("Security", "Security." + name, [("s", {}, False)])
                self.assertIsInstance(result.value[0], Success, result)
                self.assertEqual(result.value[0].value, expected)
            for name, status in [
                ("overflow", Status.ERROR),
                ("preserved", Status.EMPTY),
            ]:
                self.assertEqual(
                    reader.calculate_many("Security", "Security." + name, [("s", {}, False)])
                    .value[0]
                    .status,
                    status,
                )
            for name, expected in [("filtered", Decimal(3)), ("expanded", Decimal(10))]:
                result = reader.calculate_many(
                    "Fund",
                    "Fund." + name,
                    [("f", {"as_of_date": date(2026, 1, 10)}, False)],
                )
                self.assertEqual(result.value[0].value, expected)
            results = reader.calculate_many(
                "Fund",
                "Fund.amount",
                [
                    ("f", {}, False),
                    ("f", {"as_of_date": "bad"}, False),
                    (1, {"as_of_date": date(2026, 1, 10)}, False),
                    ("f", {"as_of_date": date(2026, 1, 10)}, "bad"),
                    ("missing", {}, False),
                    ("f", {"as_of_date": date(2026, 1, 10)}, False),
                ],
            )
            self.assertIsInstance(results, Success)
            self.assertEqual(
                [r.status for r in results.value[:-1]],
                [Status.ERROR] * 4 + [Status.NO_ENTRY],
            )
            self.assertEqual(dict(results.value[-1].value), {"Bank": Decimal(15)})
            with patch.object(reader._backend, "rows_many", side_effect=OSError("unavailable")):
                failure = reader.calculate_many("Security", "Security.safe", [("new", {}, False)])
                self.assertIsInstance(failure, NonNormalResult)
                self.assertEqual(failure.status, Status.ERROR)

    def test_complete_property_cache_supports_new_computation_after_close(self):
        for reader in self.readers():
            security = reader.get_security("s").value
            self.assertIsInstance(security.price, Success)
            self.assertIsInstance(security.days, Success)
            # Seed hierarchy metadata independently, without evaluating a computed field.
            self.assertIsInstance(
                reader._computing.available.hierarchy(["Local"])["Local"], Success
            )
            holding = reader.get_holding("h1").value
            self.assertIsInstance(holding.quantity, Success)
            self.assertIsInstance(holding.security, Success)
            reader.close()
            result = holding.read_amount(as_of_date=date(2026, 1, 10))
            self.assertIsInstance(result, Success, result)
            self.assertEqual(result.value, Decimal(6))

    def test_selection_windows_states_priority_and_corruption(self):
        price = self.layout.fields["Security.price"]
        self.put("Security.price", "bad", id="s", dim_source="X", date="2026-01-08")
        self.writer.commit()
        reader = next(self.readers())
        available = reader._computing.available
        point = FieldRequest(EntityRef("Security", "s"), "Security.price", (), date(2026, 1, 10))
        self.assertEqual(available.read([BestRequest(point, (), 3)])[0].value, Decimal(3))
        source = reader.code_types["Sources"]("X")
        explicit = replace(point, dimensions=(("source", source),))
        self.assertEqual(
            available.read([BestRequest(explicit, (("hierarchy", "Absent"),), 3)])[0].value,
            Decimal("2.5"),
        )
        self.assertEqual(available.read([BestRequest(point, (), -1)])[0].status, Status.ERROR)
        self.assertEqual(
            available.read([BestRequest(point, (("hierarchy", "Absent"),))])[0].status,
            Status.ENTITY_NOT_FOUND,
        )
        for days, expected in [
            (0, Status.NO_ENTRY),
            (1, Status.ERROR),
            (10**100, Status.ERROR),
        ]:
            past = replace(explicit, when=date(2026, 1, 8))
            if days == 0:
                past = replace(past, when=date(2026, 1, 7))
            self.assertEqual(available.read([BestRequest(past, (), days)])[0].status, expected)
        for state, expected in [
            ("NO_ENTRY", Status.NO_ENTRY),
            ("EMPTY", Status.NO_DATA),
            ("NOT_APPLICABLE", Status.NO_DATA),
        ]:
            with patch.object(
                reader._backend,
                "rows_many",
                return_value=[
                    [
                        {
                            "id": "s",
                            "dim_source": "X",
                            "date": "2026-01-10",
                            price.state: state,
                            price.value: None,
                        }
                    ]
                ],
            ):
                candidate = Available(reader._batch)
                self.assertEqual(candidate.read([BestRequest(explicit)])[0].status, expected)
        industry = FieldRequest(
            EntityRef("Issuer", "i"),
            "Issuer.industry",
            (("industry_type", self.module.IndustriesCode.Primary),),
            date(2026, 1, 11),
        )
        self.assertEqual(available.read([BestRequest(industry)])[0].value, "Technology")
        self.assertEqual(
            available.read([BestRequest(replace(industry, dimensions=()))])[0].status,
            Status.ERROR,
        )
        self.assertEqual(
            available.read([BestRequest(replace(point, identity="Security.absent"))])[0].status,
            Status.NOT_DEFINED,
        )
        with patch.object(
            available,
            "dimensions",
            return_value=(
                replace(
                    available.dimensions(reader._implementations["Security.price"])[0],
                    hierarchy_default=None,
                ),
            ),
        ):
            self.assertEqual(
                available.read([BestRequest(replace(point, when=date(2026, 1, 12)))])[0].status,
                Status.ERROR,
            )
        stored = self.layout.fields["Issuer.industry"]
        rows = [
            {
                "id": "i",
                "dim_industry_type": "Primary",
                "valid_from": "2026-01-01",
                "valid_to": "2999-12-31",
                stored.state: "VALUE",
                stored.value: "A",
            }
        ] * 2
        with patch.object(reader._backend, "rows_many", return_value=[rows]):
            self.assertEqual(
                Available(reader._batch).read([BestRequest(industry)])[0].status,
                Status.ERROR,
            )

    def test_hierarchy_validation_retains_arbitrary_integer_order(self):
        reader = next(self.readers())
        header = {"hierarchy_entries__state": "VALUE"}
        for headers, members, expected in [
            ([], [], Status.ENTITY_NOT_FOUND),
            ([header, header], [], Status.ERROR),
            ([{"hierarchy_entries__state": "EMPTY"}], [], Status.EMPTY),
            ([{"hierarchy_entries__state": "bad"}], [], Status.ERROR),
            ([header], [{"entry_value": "X", "ordering": "01"}], Status.ERROR),
            (
                [header],
                [
                    {"entry_value": "X", "ordering": "1"},
                    {"entry_value": "Y", "ordering": "1"},
                ],
                Status.ERROR,
            ),
            ([header], [{"entry_value": "X", "ordering": "bad"}], Status.ERROR),
        ]:
            with patch.object(reader._backend, "rows_many", side_effect=[[headers], [members]]):
                self.assertEqual(
                    Available(reader._batch).hierarchy(["Test"])["Test"].status,
                    expected,
                )
        members = [
            {"entry_value": "Y", "ordering": str(10**50)},
            {"entry_value": "X", "ordering": str(-(10**50))},
        ]
        with patch.object(reader._backend, "rows_many", side_effect=[[[header]], [members]]):
            self.assertEqual(Available(reader._batch).hierarchy(["Test"])["Test"].value, ("X", "Y"))

    def test_result_boundaries_and_lazy_failure_paths(self):
        reader = next(self.readers())
        engine = reader._computing
        lane = (0,)
        owner = {lane: Success(EntityRef("Security", "s"))}
        frame = Frame((lane,), owner, {}, (False,))

        def Type(kind, name, arguments=()):
            return replace(
                reader._implementations["Security.days"].type,
                kind=kind,
                name=name,
                arguments=arguments,
            )

        integer = Type("value", "integer")
        decimal = Type("value", "decimal")
        for type_, value in [
            (
                Type(
                    "collection",
                    "mapping",
                    arguments=(Type("string", "string"), integer),
                ),
                1,
            ),
            (Type("collection", "values", arguments=(integer,)), 1),
            (Type("value", "float"), "bad"),
            (Type("value", "float"), float("inf")),
        ]:
            with self.assertRaises(ValueError):
                engine.boundary(type_, value)
        self.assertEqual(
            engine.boundary(Type("collection", "values", arguments=(integer,)), (1, 2, 1)),
            frozenset({1, 2}),
        )
        self.assertEqual(engine.boundary(decimal, 1), Decimal(1))
        self.assertEqual(engine.boundary(Type("value", "float"), 1), 1.0)
        for value, status in [
            (NonNormalResult(Status.EMPTY), Status.EMPTY),
            (Success(1), Status.ERROR),
        ]:
            result = engine.choose(
                frame,
                {lane: value},
                lambda _: self.fail("lazy true branch"),
                lambda _: self.fail("lazy false branch"),
            )
            self.assertEqual(result[lane].status, status)
        self.assertEqual(
            engine.field(frame, {lane: Success(1)}, "Security.days", "at", {})[lane].status,
            Status.ERROR,
        )
        self.assertEqual(
            engine.field(
                frame,
                {lane: NonNormalResult(Status.NO_ENTRY)},
                "Security.days",
                "at",
                {},
            )[lane].status,
            Status.NO_ENTRY,
        )
        self.assertEqual(
            engine.field(frame, owner, "Security.absent", "at", {})[lane].status,
            Status.NOT_DEFINED,
        )
        self.assertEqual(
            engine.compute(frame, owner, "Security.days", {})[lane].status,
            Status.NOT_DEFINED,
        )
        for binding in [
            {"source": NonNormalResult(Status.EMPTY)},
            {"hierarchy": Success(1)},
            {"as_of_date": NonNormalResult(Status.EMPTY)},
            {"as_of_date": Success("bad")},
        ]:
            args = {
                "as_of_date": {lane: Success(date(2026, 1, 10))},
                **{k: {lane: v} for k, v in binding.items()},
            }
            self.assertIsInstance(
                engine.field(frame, owner, "Security.price", "best_available", args)[lane],
                NonNormalResult,
            )
        for value in [Success("bad"), NonNormalResult(Status.EMPTY)]:
            with patch.dict(
                engine.programs,
                {
                    "fallback:Security.price:prices": lambda e, f: (
                        e.literal(f, value.value)
                        if isinstance(value, Success)
                        else {l: value for l in f.lanes}
                    )
                },
            ):
                self.assertIsInstance(
                    engine.field(
                        frame,
                        owner,
                        "Security.price",
                        "best_available",
                        {"as_of_date": {lane: Success(date(2026, 1, 10))}},
                    )[lane],
                    NonNormalResult,
                )
        failed = engine.compute(
            Frame(
                (lane,),
                owner,
                {"as_of_date": {lane: NonNormalResult(Status.EMPTY)}},
                (False,),
            ),
            owner,
            "Security.industry",
            {},
        )
        self.assertEqual(failed[lane].status, Status.EMPTY)
        partial = Partial(True, (CalculationIssue(Status.NO_ENTRY),))
        selected = engine.choose(
            frame,
            {lane: partial},
            lambda f: engine.literal(f, 5),
            lambda _: self.fail("false branch"),
        )
        self.assertIsInstance(selected[lane], Partial)
        for grouped in [False, True]:
            source = {lane: Success(Groups((("k", (((0, 0), 1),)),)) if grouped else (1,))}
            total = engine.sequence(frame, source, "sum_by", lambda f, v: engine.literal(f, "bad"))
            self.assertEqual(total[lane].status, Status.ERROR)
        located = engine.annotated(
            NonNormalResult(Status.NO_ENTRY),
            replace(
                frame,
                inputs={
                    "day": {lane: Success(date(2026, 1, 1))},
                    "omitted": {lane: Success(UNSET)},
                },
            ),
            owner,
            lane,
            "Security.price",
            {},
        )
        self.assertEqual(located.issues[0].coordinates, (("day", "2026-01-01"),))

    def test_batch_validation_complete_scd_cache_and_query_chunking(self):
        readers = list(self.readers())
        reader, files = readers
        batch = reader._batch
        self.assertEqual(reader.get_bond_many(["s"]).value[0].status, Status.NO_ENTRY)
        self.assertEqual(
            batch.entities([EntityRef("Bond", "s")])[0].status, Status.ENTITY_NOT_FOUND
        )
        for rows in [
            [{"entity_type": "Security"}] * 2,
            [{"entity_type": "Invalid"}],
            [{"entity_type": "Issuer"}],
        ]:
            with patch.object(reader._backend, "rows_many", return_value=[rows]):
                self.assertEqual(
                    batch.entities([EntityRef("Security", "new")])[0].status,
                    Status.ERROR,
                )
        point = FieldRequest(EntityRef("Security", "s"), "Security.price")
        field = reader._implementations["Security.price"]
        stored = self.layout.fields["Security.price"]
        with self.assertRaises(ValueError):
            batch.selection(point, stored, field)
        with self.assertRaises(ValueError):
            batch.selection(
                replace(point, dimensions=(("source", reader.code_types["Sources"]("X")),)),
                stored,
                field,
            )
        self.assertEqual(
            batch.read([replace(point, identity="absent")])[0].status,
            Status.NOT_DEFINED,
        )
        issuer = reader.get_issuer("i").value
        self.assertIsInstance(issuer.industry, Success)
        industry = FieldRequest(
            EntityRef("Issuer", "i"),
            "Issuer.industry",
            (("industry_type", self.module.IndustriesCode.Primary),),
            date(2026, 1, 11),
        )
        self.assertEqual(batch.read([industry])[0].value, "Technology")
        self.assertEqual(
            reader._computing.available.read([BestRequest(industry)])[0].value,
            "Technology",
        )
        security = reader.get_security("s").value
        self.assertIsInstance(security.price, Success)
        explicit = replace(
            point,
            dimensions=(("source", reader.code_types["Sources"]("X")),),
            when=date(2026, 1, 9),
        )
        self.assertEqual(
            reader._computing.available.read([BestRequest(explicit)])[0].value,
            Decimal("2.5"),
        )
        self.assertEqual(
            reader._computing.available.read(
                [BestRequest(replace(explicit, when=date(2026, 1, 10)))]
            )[0].status,
            Status.NO_ENTRY,
        )
        self.assertEqual(
            reader._computing.available.read(
                [BestRequest(FieldRequest(EntityRef("Security", "s"), "Security.issuer"))]
            )[0].value.id,
            "i",
        )
        self.assertEqual(
            len(
                reader._computing.available.read(
                    [
                        BestRequest(
                            FieldRequest(
                                EntityRef("Fund", "f"),
                                "Fund.holdings",
                                (),
                                date(2026, 1, 10),
                            )
                        )
                    ]
                )[0].value
            ),
            2,
        )
        lane = (0,)
        owner = {lane: Success(EntityRef("Issuer", "i"))}
        frame = Frame((lane,), owner, {"as_of_date": {lane: Success(date(2026, 1, 11))}}, (False,))
        self.assertEqual(
            reader._computing.field(
                frame,
                owner,
                "Issuer.industry",
                "best_available",
                {"industry_type": {lane: Success(self.module.IndustriesCode.Primary)}},
            )[lane].value,
            "Technology",
        )
        self.assertEqual(files._backend.rows_many("tb_security", []), [])
        connection = reader._backend.connection
        original = connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3)
        try:
            rows = reader._backend.rows_many(
                "tb_security", [Selection((("id", "s"),))] * 5, ("id",)
            )
            self.assertEqual(rows, [[{"id": "s"}]] * 5)
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)
            with self.assertRaises(ValueError):
                reader._backend.rows_many("tb_security", [Selection((("id", "s"),))])
        finally:
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original)
        self.assertEqual(reader.calculate(security, "Security.safe", {}).value, 10)
        self.assertEqual(files.calculate(security, "Security.safe", {}).status, Status.ERROR)
        with self.assertRaises(ValueError):
            reader._id_type("missing")
        with patch.object(
            reader._backend, "rows_many", return_value=[[{"entity_type": "Invalid"}]]
        ):
            self.assertEqual(reader.get_security_many(["bad"]).status, Status.ERROR)
        stored = self.layout.fields["Fund.members"]
        field = reader._implementations["Fund.members"]
        row = {
            "id": "f",
            "valid_from": "2026-01-01",
            "valid_to": "2999-12-31",
            stored.state: "VALUE",
        }
        with patch.object(reader._backend, "rows_many", return_value=[[{"member_id": None}]]):
            batch.collections([(stored, field, row)])
            self.assertEqual(row["_result"].status, Status.ERROR)

    def test_generator_rejects_names_and_malformed_calculation_plans(self):
        field = next(
            f
            for d in self.model.definitions
            for f in d.fields
            if f.implementation == "Security.safe"
        )
        emitter = Emitter(self.model, field)
        node = field.calculation.body.result
        for invalid in [
            replace(node, kind="name", value="unbound", operands=()),
            replace(
                node,
                kind="member",
                value="missing",
                operands=(replace(node, kind="name", value="self", operands=()),),
            ),
            replace(node, kind="lambda"),
        ]:
            with self.assertRaises(ValueError):
                emitter.expression(invalid, "frame", {})
        missing = replace(
            node,
            kind="member",
            value="missing",
            operands=(replace(node, kind="name", value="self", operands=()),),
        )
        with self.assertRaises(ValueError):
            emitter.fallback_accesses(missing, "Security")
        with self.assertRaises(ValueError):
            emitter.fallback_accesses(replace(missing, value="price"), "Security")
        emitter.fallback_accesses(node, "Security")
        p = next(
            f.calculation.inputs[0]
            for d in self.model.definitions
            for f in d.fields
            if f.implementation == "Security.industry"
        )
        default = replace(
            node,
            kind="member",
            value=p.name,
            operands=(replace(node, kind="name", value="input", operands=()),),
        )
        bad = replace(
            field,
            calculation=replace(field.calculation, inputs=(replace(p, default=default),)),
        )
        model = replace(
            self.model,
            definitions=tuple(
                replace(d, fields=tuple(bad if f == field else f for f in d.fields))
                for d in self.model.definitions
            ),
        )
        with self.assertRaisesRegex(ValueError, "default cycle"):
            programs(model)
        for declarations, message in [
            (
                "entity E { id key: string snapshot attr x: integer computed attr y: integer { return self.x::at(as_of_date=input.id_value) } }",
                "parameter collision",
            ),
            (
                "entity E { id key: string snapshot attr x: integer computed attr x_range: integer { return 1 } }",
                "method collision",
            ),
            (
                "entity E { id key: string static attr read_total: string computed attr total: integer { return 1 } }",
                "method collision",
            ),
        ]:
            self.write("main.yambl", declarations)
            with self.assertRaisesRegex(ValueError, message):
                python_package(build_layout(compile_repository(self.root)), "example_model")
