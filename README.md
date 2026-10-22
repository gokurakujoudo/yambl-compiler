# YAMBL compiler

Static checks and formatting for [YAMBL](https://github.com/gokurakujoudo/yambl),
using Python 3.14+, Lark 1.3.1, and lclang 1.1.2.

## Install and run

From this repository in PowerShell, use the existing virtual environment:

```powershell
.\venv\Scripts\python.exe -m pip install -e '.[dev]'
.\venv\Scripts\yambl.exe check -o path ..\fmc-data-model
.\venv\Scripts\yambl.exe format -o path ..\fmc-data-model -o check
.\venv\Scripts\yambl.exe format -o path ..\fmc-data-model
```

For a fresh clone, create the environment with `python -m venv venv` first.
With that environment activated, `yambl` and `python -m yambl_compiler` are
equivalent entry points. `yambl check --help` and `yambl format --help` list inputs.

Commands use `lclang.cli` overrides: `-o path PATH` selects a file or directory;
the default path is the current directory. `format -o check` checks for formatting
drift without writing. `format --dryrun` has the same read-only behavior. Put a
valueless override last, or before another option. lclang configuration files and
typed `LCL[...]` overrides are also supported; only load configuration you trust.

Exit status is 0 for success, 1 for invalid sources, I/O failures, or formatting
drift, and 2 for CLI usage or unexpected runtime errors. Source diagnostics and
summaries go to stdout; lclang workflow logs go to stderr. A dry run that finds
formatting drift returns 1.

## Checks

The checker supports entity and Decodes declarations, fields, independent temporal
snapshots, primitive parameters, collection generics, namespaces, and imports.
It checks naming conventions, duplicate declarations and fields, Decodes entry
identity, imports, references, and built-in type constraints. Diagnostics include
the source path, line, and column. Each file reports its first syntax error;
semantic diagnostics are collected after every file parses.

Namespace declarations determine scope independently of directories. Files in
the same namespace share declarations. Cross-namespace imports apply only to the
importing file. Circular entity references are supported.

Checking a single file searches its parents for the nearest `yambl-root.lclcfg`.
If found, all YAMBL sources beneath that root form the compilation unit, and
diagnostics can include other files in that unit. Without a marker, only the
selected file is checked. A directory input checks the subtree explicitly given.
Hidden directories, virtual environments, `build`, `dist`, `__pycache__`, and
symbolic links encountered during directory scans are excluded. Root marker
contents are not executed; `prefix` has no implemented semantics yet.

Current implementation limits:

- A file has at most one namespace, before imports and declarations. Omitting it
  places declarations in the compilation unit's unnamed namespace.
- Imports accept one name, without aliases or wildcards. Import collisions fail.
- Identifiers use ASCII letters, digits, and underscores. Strings use single
  quotes and may contain backslash escapes; their spelling is preserved without
  interpreting escapes or choosing a regular-expression dialect.
- `snowflake_id` is a built-in value type. Its bit layout and generation policy
  are not specified. Unresolved types fail; custom value-type declarations are
  not defined yet.
- Decodes names are expected to be plural; the checker validates PascalCase but
  does not attempt linguistic plural detection.
- Static checks describe model declarations. They do not validate business data
  completeness, snapshot consistency, regex matches, or stored collection values.

## Formatting

Formatting parses all selected sources before writing. It does not require
resolved references, so incomplete model designs can still be formatted. Selecting
a file formats only that file, even when it belongs to a repository.

Output uses UTF-8 without BOM, LF endings, four-space indentation, normalized
punctuation spacing, two spaces before inline comments, and one space after `#`.
It preserves string spelling, comment text, declaration order, and at most one
existing blank separator. Every file ends with exactly one LF character (`\n`),
so an editor shows one empty final line. Extra trailing blank lines are removed.
Empty files also normalize to a single LF.

Each changed file is written through a temporary file in the same directory and
atomically replaced. A syntax or decoding failure prevents all writes. An I/O
failure during publication stops the run; earlier replacements remain in place.
Avoid concurrent edits while formatting. Repeating a successful run changes nothing.

## Development

The parser retains a concrete syntax tree and source positions. The formatter
uses those positions to preserve comments; the validator resolves a compilation
unit in two passes. The filesystem layer owns discovery and publication. Thin
lclang workflows expose the `check` and `format` commands.

Source and unit tests use the same functional package structure:

| Source package under `src/yambl_compiler/` | Matching tests | Responsibility |
| --- | --- | --- |
| `syntax/parser.py`, `syntax/grammar.lark` | `tests/syntax/test_parser.py` | Tokens, parse trees, source diagnostics |
| `semantics/validation.py` | `tests/semantics/test_validation.py` | Namespaces, references, type constraints |
| `formatting/formatter.py` | `tests/formatting/test_formatter.py` | Layout and comment preservation |
| `application/project.py` | `tests/application/test_project.py` | File discovery and publication |
| `application/cli.py` | `tests/application/test_cli.py` | lclang CLI workflows |

`tests/support.py` holds shared fixtures. Documentation checks cover the whole
package in `tests/test_documentation.py`.

Run the same gates used by CI:

```console
python -m black --check src tests
python -m ruff check src tests
python -m mypy
python -m pyright
python -m coverage erase
python -m coverage run -m unittest discover -s tests -v
python -m coverage report
python -m coverage json -o build/coverage.json
```

Tests cover source diagnostics, import scope, formatting idempotence, exact file
endings, publication failures, and the real lclang entrance. Documentation tests
check rST syntax and parameter descriptions. CI requires zero missing statements
and branches in every application module.

A stable intermediate representation and target generation are future work.
Planned targets include database schemas, data interfaces, and Python libraries.
The current business example lives in
[fmc-data-model](https://github.com/gokurakujoudo/fmc-data-model).
