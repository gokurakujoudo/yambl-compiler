"""Check explicit constraint reports for stored and calculated result states."""

import sqlite3
from contextlib import closing

from tests.support import FilesystemCase, load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.records import quote
from yambl_compiler.storage.results import CalculationIssue, Partial, Status
from yambl_compiler.transactions.snapshot import SnapshotBackend


class ConstraintReportTests(FilesystemCase):
    def test_reports_native_results_and_partial_without_source_files(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        source = self.write(
            'model.yambl',
            '''entity A {
    id id: string
    attr n: integer
    constraint states {
        assert self.n::value() > 0
        assert NO_ENTRY
        assert ERROR
        assert true
    }
}''',
        )
        output = self.root / 'generated'
        layout = generate(compile_repository(self.root), output, 'sample_model')
        module = load_generated(output / 'sample_model', 'sample_model')
        with closing(sqlite3.connect(':memory:')) as database:
            database.row_factory = sqlite3.Row
            database.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
            database.execute("INSERT INTO tb_a (id, n__state) VALUES ('a', 2)")
            tables = {
                table.name: [
                    dict(row) for row in database.execute(f'SELECT * FROM {quote(table.name)}')
                ]
                for table in layout.tables
            }
        source.unlink()
        with module.SampleReader(SnapshotBackend(tables), layout) as reader:
            report = reader.validate().value
            issues = [issue for issue in report.issues if issue.constraint]
            self.assertEqual([issue.status for issue in issues], ['EMPTY', 'NO_ENTRY', 'ERROR'])
            self.assertEqual(
                [issue.assert_source for issue in issues],
                ['assert self.n::value() > 0', 'assert NO_ENTRY', 'assert ERROR'],
            )
            original = reader.constraint_programs['A::constraint.states']

            def partial(engine, frame):
                results = list(original(engine, frame))
                results[-1] = ({(0,): Partial(True, (CalculationIssue(Status.EMPTY),))}, {})
                return tuple(results)

            reader.constraint_programs = {
                **reader.constraint_programs,
                'A::constraint.states': partial,
            }
            issues = [issue for issue in reader.validate().value.issues if issue.constraint]
            self.assertEqual(issues[-1].status, 'PARTIAL')
        tables['tb_a'][0]['n__state'] = 0
        tables['tb_a'][0]['n'] = 'bad'
        with module.SampleReader(SnapshotBackend(tables), layout) as reader:
            issues = [issue for issue in reader.validate().value.issues if issue.constraint]
            self.assertEqual(issues[0].status, 'ERROR')
