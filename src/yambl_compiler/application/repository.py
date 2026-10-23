"""Read static repository prefixes and locate model roots."""

import re
from pathlib import Path

ROOT_NAME = 'yambl-root.lclcfg'
PREFIX_LINE = re.compile(
    r'''^\s*prefix\s*:\s*(['"])([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*|)\1\s*(?:\#.*)?$'''
)


def find_root(path: Path) -> Path | None:
    """Find the nearest model repository containing a path.

    :param path: Existing source file or directory.
    :returns: Nearest root, or None for standalone input.
    """
    directory = path if path.is_dir() else path.parent
    return next((p for p in (directory, *directory.parents) if (p / ROOT_NAME).is_file()), None)


def read_prefix(path: Path) -> str:
    """Read one literal prefix without executing configuration expressions.

    :param path: Root configuration file.
    :returns: Empty or qualified namespace prefix.
    :raises ValueError: If configuration is missing, duplicated or unsupported.
    """
    prefix: str | None = None
    for line_number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = PREFIX_LINE.fullmatch(line)
        if match is None or prefix is not None:
            raise ValueError(
                f'{path}:{line_number}:1: error: expected one literal prefix declaration'
            )
        prefix = match[2]
    if prefix is None:
        raise ValueError(f'{path}:1:1: error: missing prefix declaration')
    return prefix
