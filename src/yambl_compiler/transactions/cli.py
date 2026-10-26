"""Expose configured data operations through lclang workflows."""

import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from lclang.cli import CliEntrance, CommandGroup
from lclang.workflow import (
    ExecutionStatus,
    ExecutionStatusManager,
    TaskContext,
    define_task,
    define_variable,
    define_workflow,
)

from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.yaplan.model import Plan
from yambl_compiler.languages.yatran.model import Transaction
from yambl_compiler.storage.results import Success
from yambl_compiler.transactions.repository import DataStore


@dataclass(frozen=True, kw_only=True)
class Arguments:
    """Supply one operation on a configured repository.

    :param operation: Selected data operation.
    :param config: Entry repository configuration filename.
    :param source: Plan or Transaction filename for a write operation.
    :param groups: Rollback group names; empty selects all groups.
    """

    operation: Literal['create', 'apply', 'rollback', 'check_data']
    config: str
    source: str = ''
    groups: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Outcome:
    """Expose operation success to the workflow result.

    :param ok: Whether the operation or read-only validation succeeded.
    """

    ok: bool


def commands(data_type: type[DataStore]) -> CommandGroup:
    """Bind workflows to one generated data facade without global mutable state.

    :param data_type: Generated model-specific data interface.
    :returns: Native command group for lclang.
    """
    config = define_variable[str]('config', 'Required repository .lclcfg entry file.')
    source = define_variable[str]('source', 'Required .yaplan or .yatran input file.')
    groups = define_variable[tuple[str, ...]](
        'groups', "Rollback group names, e.g. LCL[['prices']]; default: all groups.", default=()
    )

    async def execute(
        context: TaskContext, args: Arguments, status_mgr: ExecutionStatusManager
    ) -> Outcome:
        """Run one complete public data operation.

        :param context: Workflow dry-run flag and services.
        :param args: Bound command arguments.
        :param status_mgr: Owning task's execution status.
        :returns: Operation outcome; also prints details and records task status.
        """
        if context.is_dryrun and args.operation != 'check_data':
            with await data_type.configured(Path(args.config)) as data:
                if args.operation == 'create':
                    if data.path.exists():
                        raise FileExistsError(data.path)
                else:
                    document = parse(Path(args.source).read_text(encoding='utf-8'))
                    if args.operation == 'apply' and not isinstance(document, Plan):
                        raise ValueError('apply requires a Plan')
                    if args.operation == 'rollback' and not isinstance(document, Transaction):
                        raise ValueError('rollback requires a Transaction')
                print(json.dumps({'status': 'dry_run', 'operation': args.operation}))
            status_mgr.update(ExecutionStatus.SUCCESS)
            return Outcome(ok=True)
        opener = (
            data_type.create
            if args.operation == 'create'
            else data_type.configured if context.is_dryrun else data_type.open
        )
        with await opener(Path(args.config)) as data:
            if args.operation == 'create':
                print(json.dumps({'status': 'created'}))
            elif args.operation == 'check_data':
                reader = data.read()
                try:
                    result = reader.validate()
                    valid = isinstance(result, Success) and result.value.valid
                    print(
                        json.dumps(
                            {'valid': valid, **asdict(result)}, ensure_ascii=False, default=str
                        )
                    )
                    if not valid:
                        status_mgr.update(ExecutionStatus.FAILURE)
                        return Outcome(ok=False)
                finally:
                    reader.close()
            else:
                report = (
                    data.apply(Path(args.source))
                    if args.operation == 'apply'
                    else data.rollback(Path(args.source), args.groups or None)
                )
                print(json.dumps(asdict(report), ensure_ascii=False, default=str))
        status_mgr.update(ExecutionStatus.SUCCESS)
        return Outcome(ok=True)

    create_task = define_task(
        'create_repository',
        'Initialize a new configured repository',
        task_action=execute,
        args_mapping=Arguments(operation='create', config=config.quote),
    )
    apply_task = define_task(
        'apply_plan',
        'Apply a complete Plan atomically',
        task_action=execute,
        args_mapping=Arguments(operation='apply', config=config.quote, source=source.quote),
    )
    rollback_task = define_task(
        'rollback_transaction',
        'Roll back selected transaction groups',
        task_action=execute,
        args_mapping=Arguments(
            operation='rollback',
            config=config.quote,
            source=source.quote,
            groups=groups.quote,
        ),
    )
    check_data_task = define_task(
        'validate_data',
        'Validate the configured repository',
        task_action=execute,
        args_mapping=Arguments(operation='check_data', config=config.quote),
    )
    return CommandGroup(
        'data',
        'Read configuration and operate on model data.',
        (
            define_workflow('Create data repository', create_task).to_cli(
                'create', 'Create a new repository; existing targets are rejected.'
            ),
            define_workflow('Apply data Plan', apply_task).to_cli(
                'apply', 'Apply a Plan and print the committed transaction report.'
            ),
            define_workflow('Roll back data transaction', rollback_task).to_cli(
                'rollback', 'Preserve later changes and report skipped conflicts.'
            ),
            define_workflow('Validate model data', check_data_task).to_cli(
                'check_data', 'Validate stored data using the model rules.'
            ),
        ),
    )


def main(data_type: type[DataStore], arguments: list[str] | None = None) -> int:
    """Run generated data workflows through the native lclang CLI.

    Dry-run checks configuration and input syntax without publishing or recovering
    files. For check-data it performs the normal read-only validation.

    :param data_type: Generated data interface.
    :param arguments: Command arguments, or None for the process command line.
    :returns: Native lclang exit status; committed reports remain successful.
    """
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments and arguments[0] == 'check-data':
        arguments[0] = 'check_data'
    entrance = CliEntrance(commands(data_type))
    argv = [sys.executable, str(data_type.schema_path.with_name('__main__.py')), *arguments]
    return asyncio.run(entrance.run(argv))
