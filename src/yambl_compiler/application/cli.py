"""Expose checks and formatting through lclang workflow CLI commands."""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lclang.cli import CliEntrance, scan_commands
from lclang.workflow import (
    ExecutionStatus,
    ExecutionStatusManager,
    TaskContext,
    define_task,
    define_variable,
    define_workflow,
)

from yambl_compiler.application.project import Report, run


@dataclass(frozen=True, kw_only=True)
class Arguments:
    """Supply a single filesystem operation.

    :param path: Source file or directory.
    :param operation: Selected CLI command.
    :param verify: Whether formatting only checks for drift.
    """

    path: str
    operation: Literal['check', 'format']
    verify: bool = False


async def execute(
    context: TaskContext, args: Arguments, status_mgr: ExecutionStatusManager
) -> Report:
    """Run the selected operation and expose its result to lclang.

    :param context: Workflow services and dry-run flag.
    :param args: Validated command arguments.
    :param status_mgr: Owning task status manager.
    :returns: Operation report; also prints diagnostics and updates task status.
    """
    report = run(Path(args.path), args.operation, args.verify or context.is_dryrun)
    for message in report.messages:
        print(message)
    status_mgr.update(ExecutionStatus.SUCCESS if report.ok else ExecutionStatus.FAILURE)
    return report


path_variable = define_variable[str](
    'path', 'YAMBL file or directory; default: current directory.', default='.'
)
verify_variable = define_variable[bool](
    'check',
    'Report formatting drift without writing; default: False. Enable with -o check.',
    default=False,
)

check_command = define_workflow(
    'Check YAMBL syntax and references',
    define_task(
        'check_sources',
        'Validate the compilation unit',
        task_action=execute,
        args_mapping=Arguments(path=path_variable.quote, operation='check'),
    ),
).to_cli('check', 'Check syntax, names and built-in type constraints; errors fail the run.')

format_command = define_workflow(
    'Format YAMBL sources',
    define_task(
        'format_sources',
        'Format selected sources after all files parse',
        task_action=execute,
        args_mapping=Arguments(
            path=path_variable.quote, operation='format', verify=verify_variable.quote
        ),
    ),
).to_cli('format', 'Format sources in place; -o check or --dryrun checks drift without writing.')


def main(argv: list[str] | None = None) -> int:
    """Run the CLI in a new event loop.

    :param argv: Complete lclang token array, or None to read process arguments.
    :returns: Zero on success, one on failed checks, two on CLI/runtime errors.
    """
    application = CliEntrance(
        scan_commands(sys.modules[__name__], name='yambl', description='YAMBL source tools'),
        version='0.1.0',
    )
    if argv is None:
        # lclang 1.1.2 expects a Python script path, including for console-script launchers.
        argv = [sys.executable, str(Path(__file__).parents[1] / '__main__.py'), *sys.argv[1:]]
    return asyncio.run(application.run(argv))
