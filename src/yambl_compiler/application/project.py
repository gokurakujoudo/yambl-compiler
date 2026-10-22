"""Read compilation units and publish formatted files with atomic replacement."""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never

from yambl_compiler.formatting.formatter import format_document
from yambl_compiler.semantics.validation import check
from yambl_compiler.syntax.parser import Diagnostic, Document, parse


@dataclass(frozen=True)
class Report:
    """Describe one complete check or formatting run.

    :param ok: Whether the requested operation succeeded.
    :param messages: Diagnostics and summaries for the caller to display.
    """

    ok: bool
    messages: list[str]


def source_files(path: Path, dependencies: bool) -> list[Path]:
    """Select sources, optionally expanding a file to its enclosing repository.

    :param path: Existing YAMBL file or directory.
    :param dependencies: Whether to find the nearest root marker for single-file checks.
    :returns: Sorted source paths, excluding environments and generated directories.
    :raises ValueError: If the input is missing, has another extension, or contains no sources.
    :raises OSError: If any selected directory cannot be scanned.
    """
    path = path.resolve()
    if path.is_file():
        if path.suffix != '.yambl':
            raise ValueError(f'expected a .yambl file: {path}')
        if dependencies:
            for parent in path.parents:
                if (parent / 'yambl-root.lclcfg').is_file():
                    path = parent
                    break
        if path.is_file():
            return [path]
    if not path.is_dir():
        raise ValueError(f'directory or file does not exist: {path}')
    excluded = {'venv', '.venv', 'build', 'dist', '__pycache__'}
    result: list[Path] = []

    def scan_failed(error: OSError) -> Never:
        """Prevent an incomplete directory scan from appearing successful.

        :param error: Filesystem failure reported by Path.walk.
        :returns: Never returns normally.
        :raises OSError: Re-raises the original scan failure.
        """
        raise error

    for directory, subdirs, files in path.walk(on_error=scan_failed):
        subdirs[:] = [name for name in subdirs if not name.startswith('.') and name not in excluded]
        result.extend(
            directory / name
            for name in files
            if name.endswith('.yambl') and not (directory / name).is_symlink()
        )
    if not result:
        raise ValueError(f'no .yambl sources in {path}')
    return sorted(result)


def replace_text(path: Path, text: str) -> None:
    """Replace one file without exposing partial contents.

    :param path: Existing target file; callers must serialize concurrent edits.
    :param text: Complete replacement text.
    :returns: None after the temporary file is published and cleaned up.
    :raises OSError: If writing, replacing, or cleanup fails.
    """
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.yambl-', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(text, encoding='utf-8', newline='\n')
        temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(path: Path, operation: Literal['check', 'format'], verify: bool = False) -> Report:
    """Check a compilation unit or format selected files.

    :param path: A source file or source directory.
    :param operation: Static check or formatting operation.
    :param verify: For formatting, report drift without writing anything.
    :returns: A report; failures stop writes, except completed earlier replacements on I/O failure.
    """
    messages: list[str] = []
    try:
        paths = source_files(path, dependencies=operation == 'check')
        documents: list[Document] = []
        originals: dict[Path, bytes] = {}
        for source in paths:
            originals[source] = source.read_bytes()
            parsed = parse(originals[source].decode('utf-8-sig'), source)
            if isinstance(parsed, Diagnostic):
                messages.append(str(parsed))
            else:
                documents.append(parsed)
        if messages:
            return Report(False, messages)
        if operation == 'check':
            diagnostics = check(documents)
            return Report(
                not any(item.severity == 'error' for item in diagnostics),
                [str(item) for item in diagnostics] + [f'checked {len(documents)} file(s)'],
            )
        changed = 0
        for document in documents:
            formatted = format_document(document)
            if originals[document.path] != formatted.encode('utf-8'):
                changed += 1
                if verify:
                    messages.append(f'would format: {document.path}')
                else:
                    replace_text(document.path, formatted)
                    messages.append(f'formatted: {document.path}')
        messages.append(
            f'{changed} of {len(documents)} file(s) need formatting'
            if verify
            else f'formatted {changed} of {len(documents)} file(s)'
        )
        return Report(not (verify and changed), messages)
    except (OSError, UnicodeError, ValueError) as error:
        return Report(False, messages + [str(error)])
