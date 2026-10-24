"""Provide the standard-library-only data-validation command."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from yambl_compiler.storage.results import NonNormalResult
from yambl_compiler.storage.runtime import Reader


def main(reader_type: type[Reader], arguments: list[str] | None = None) -> int:
    """Validate an existing database or JSONL repository.

    :param reader_type: Generated model-specific reader.
    :param arguments: Command arguments, or the process command line.
    :returns: Zero for valid data, one for invalid data or read failure.
    :raises SystemExit: With status two for command usage errors.
    """
    parser = argparse.ArgumentParser(description="Validate model data and reference cycles.")
    parser.add_argument("command", choices=["check-data"])
    parser.add_argument("--backend", required=True, choices=["sqlite", "files"])
    parser.add_argument("--path", required=True, type=Path)
    options = parser.parse_args(arguments)
    opened = (reader_type.open_sqlite if options.backend == "sqlite" else reader_type.open_files)(
        options.path
    )
    if isinstance(opened, NonNormalResult):
        print(json.dumps(asdict(opened), ensure_ascii=False))
        return 1
    reader = opened.value
    try:
        result = reader.validate()
        if isinstance(result, NonNormalResult):
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 1
        print(
            json.dumps(
                {"valid": result.value.valid, **asdict(result.value)},
                ensure_ascii=False,
            )
        )
        return 0 if result.value.valid else 1
    finally:
        reader.close()
