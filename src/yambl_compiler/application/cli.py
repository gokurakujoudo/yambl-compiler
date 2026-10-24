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
    :param output: Required output destination.
    :param package: Generated Python package name.
    """

    path: str
    operation: Literal['check', 'format', 'compile', 'check-ir', 'schema', 'generate']
    verify: bool = False
    output: str = ''
    package: str = ''


async def execute(
    context: TaskContext, args: Arguments, status_mgr: ExecutionStatusManager
) -> Report:
    """Run the selected operation and expose its result to lclang.

    :param context: Workflow services and dry-run flag.
    :param args: Validated command arguments.
    :param status_mgr: Owning task status manager.
    :returns: Operation report; also prints diagnostics and updates task status.
    """
    if args.operation == 'schema' or args.operation == 'generate':
        report = run_generation(args, context.is_dryrun)
    elif args.operation == 'compile' or args.operation == 'check-ir':
        report = run_ir(Path(args.path), args.operation, Path(args.output), context.is_dryrun)
    else:
        report = run(Path(args.path), args.operation, args.verify or context.is_dryrun)
    for message in report.messages:
        print(message)
    status_mgr.update(ExecutionStatus.SUCCESS if report.ok else ExecutionStatus.FAILURE)
    return report


def run_ir(path: Path, operation: str, output: Path, dry_run: bool) -> Report:
    """Run IR commands without treating output artifacts as source repositories.

    :param path: Source repository or input JSON file.
    :param operation: Compile or check-ir.
    :param output: Compile destination.
    :param dry_run: Whether compilation must avoid all publication writes.
    :returns: Command diagnostics and success state.
    """
    from yambl_compiler.ir.application import compile_repository, load_ir, write_ir

    try:
        if operation == 'check-ir':
            model = load_ir(path)
            return Report(True, [f'checked IR: {path} ({len(model.definitions)} definitions)'])
        if output.suffix.lower() != '.json':
            raise ValueError('compile requires -o output PATH ending in .json')
        model = compile_repository(path)
        messages = [
            f'{site.path}:{site.line}:{site.column}: warning: external Decodes: {external.name}'
            for external in model.external_dictionaries
            for site in external.sites
        ]
        if not dry_run:
            write_ir(model, output)
        messages.append(
            f'{"would write" if dry_run else "compiled"} IR: {output} ({len(model.definitions)} definitions)'
        )
        return Report(True, messages)
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        return Report(False, [str(error)])


def run_generation(args: Arguments, dry_run: bool) -> Report:
    """Export the schema or publish a generated backend.

    :param args: CLI arguments.
    :param dry_run: Whether publication is disabled.
    :returns: Success or a diagnostic report.
    """
    import json

    from yambl_compiler.ir.application import load_ir
    from yambl_compiler.ir.schema import schema
    from yambl_compiler.storage.generation import generate

    try:
        if not args.output:
            raise ValueError('output is required')
        output = Path(args.output)
        if args.operation == 'schema':
            if output.suffix.lower() != '.json':
                raise ValueError('schema output must end in .json')
            text = json.dumps(schema(), ensure_ascii=False, indent=4) + '\n'
            if not dry_run:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding='utf-8', newline='\n')
        else:
            generate(load_ir(Path(args.path)), output, args.package, dry_run)
        return Report(True, [f'{"validated" if dry_run else "generated"}: {output}'])
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        return Report(False, [str(error)])


path_variable = define_variable[str](
    'path', 'YAMBL file or directory; default: current directory.', default='.'
)
verify_variable = define_variable[bool](
    'check',
    'Report formatting drift without writing; default: False. Enable with -o check.',
    default=False,
)
output_variable = define_variable[str](
    'output', 'Required output: .json for compile/schema, a new directory for generate.'
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

compile_task = define_task(
    'compile_model',
    'Compile and validate a complete repository',
    task_action=execute,
    args_mapping=Arguments(
        path=path_variable.quote, operation='compile', output=output_variable.quote
    ),
)
compile_command = define_workflow('Compile a standalone JSON model', compile_task).to_cli(
    'compile', 'Compile a rooted repository to JSON IR; --dryrun validates without writing.'
)
check_ir_task = define_task(
    'check_ir',
    'Validate embedded IR definitions independently',
    task_action=execute,
    args_mapping=Arguments(path=path_variable.quote, operation='check-ir'),
)
check_ir_command = define_workflow('Validate a JSON model', check_ir_task).to_cli(
    'check_ir', 'check-ir: validate one JSON IR file without consulting its source repository.'
)


package_variable = define_variable[str]('package', 'Required snake_case package name for generate.')
schema_command = define_workflow(
    'Export the JSON IR schema',
    define_task(
        'export_schema',
        'Write the offline schema',
        task_action=execute,
        args_mapping=Arguments(path='.', operation='schema', output=output_variable.quote),
    ),
).to_cli('schema', 'Export the current JSON Schema; --dryrun does not write.')
generate_command = define_workflow(
    'Generate SQLite, JSONL and Python outputs',
    define_task(
        'generate_backend',
        'Validate and generate a new directory',
        task_action=execute,
        args_mapping=Arguments(
            path=path_variable.quote,
            operation='generate',
            output=output_variable.quote,
            package=package_variable.quote,
        ),
    ),
).to_cli('generate', 'Generate SQLite, JSONL, schema references and typed Python readers from IR.')


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
    # lclang command declarations require snake_case; keep the public hyphenated spelling.
    if len(argv) > 2 and argv[2] == 'check-ir':
        argv = [*argv[:2], 'check_ir', *argv[3:]]
    return asyncio.run(application.run(argv))
