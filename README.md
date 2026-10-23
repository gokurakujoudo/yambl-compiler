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
formatting drift returns 1. Warnings alone return 0.

## Checks

The checker supports entity and Decodes declarations, attribute extensions, single
inheritance, final entities, static, snapshot and SCD Type 2 attributes, configuration parameters, metadata,
dimensions, primitive parameters, collection generics, namespaces, and imports.
It checks naming conventions, duplicate declarations and fields, inheritance cycles,
Decodes entry identity, imports, references, and built-in type constraints. Diagnostics include
the source path, line, and column. Each file reports its first syntax error;
semantic diagnostics are collected after every file parses.

Namespace declarations determine scope independently of directories. Files in
the same namespace share declarations. Cross-namespace imports apply only to the
importing file. Circular entity references are supported.

`extend Security { ... }` merges attributes directly into `Security`. It creates
no separate entity or namespace symbol. Each merged attribute retains its metadata
and source file, line, and column; its references resolve in the declaring file's
scope. Inheritance checks use the complete merged fields, independent of file
order. Duplicate local names are errors. An inherited attribute may be redeclared
with `@override` when its signature matches. Independent branches sharing an
ancestor must mark same-name attributes with `@overload` and use matching signatures.
Overload groups need at least two independent implementations. Both markers compare
resolved types, attribute modes and metadata; config defaults may differ.
A `final entity` cannot be inherited or extended.
Extensions add attributes, not identifiers. Inheritance and extensions currently
target user-defined entity types; `Decodes`, `ConfigGroup`, and `Hierarchy` are final.

Both `code<Currencies>` and `code<'Currencies'>` name a Decodes instance. Undefined
dictionaries are allowed: each occurrence produces an `undefined Decodes: NAME`
warning with its path, line, and column. This includes uses inside generics and
`@dimension`. Missing explicit imports and unknown entity types remain errors.

`@title('...')` and `@description('...')` attach metadata to entities, Decodes
declarations, values, or fields. Each `@dimension(source: string, preference: Hierarchy)`
defines one independent dimension and an optional alternative lookup parameter.
The first parameter accepts string or code types; the second accepts Hierarchy.
Dimensions apply only to ordinary, snapshot, and scd attributes. Parameter names,
including temporal names, must be distinct within a field. Direct values take
precedence over Hierarchy references. Hierarchy tries existing data in ascending
ordering; priorities must be unique. Lookup execution is not implemented. `@temporal(as_of_date: date)` applies to `snapshot` or `scd`.
`config` attributes are time-independent and may declare a default with `= literal`.
Supported defaults are integer literals for `integer` and `snowflake_id`, numeric
literals for `float` and `decimal`, single-quoted strings for `string`, and
`true` / `false` for `bool`. Other default types and expressions are not implemented.

`ConfigGroup` is a built-in entity reference. `type<Entity>` accepts any entity
type; `type<T>` restricts the value to T and its descendants. Entity is a constraint,
not a common root entity. `field_mapping<mode, Target>` selects attributes by mode
across the target, inherited fields, extensions and descendants. Target can be an
entity name or `self.field`, where any declared field of type `type<...>` is valid,
regardless of its name or mode. Forward references and inherited or extended fields
are supported; their types resolve in their original file scopes. Self paths have
one field segment. Modes are `attr`, `static`, `config`, `snapshot`, and `scd`.
Mapping values follow each selected field's complete type. Type values are value
types; field mappings are collections and follow the existing outer-container rules.

The checker retains effective fields, overload identities and declaration locations.
It does not accept configuration-group instance syntax or evaluate mappings,
conditions, configuration precedence, or computed fields.

Checking a file or subdirectory searches upward for the nearest `yambl-root.lclcfg`
and checks the whole repository. Nested roots fail the parent check. Without a root,
the selected file or directory is checked independently; a standalone directory
containing repositories must be checked by selecting each repository separately.
Hidden directories, virtual environments, build output and symlinks are excluded.

The root configuration requires one literal `prefix: 'fmc'` declaration. Empty
prefixes, comments and blank lines are supported. Expressions, other keys and
configuration loading are rejected. A file's full namespace joins the prefix with
its namespace declaration. Imports are repository-relative: `from core import Item`
uses `fmc.core.Item`. Cross-repository dependencies are not supported.

Built-in model declarations are read from bundled `.yambl` resources. Their source
is the `yambl/models` repository, which has an empty prefix and no namespaces.
Ordinary empty-prefix repositories do not become builtins. Run
`./tools/sync-models.ps1` to refresh Python package resources from the adjacent
language repository; `-Source` accepts its model directory in another checkout.
The compiler runs without that checkout. Tests compare the resources when it is present.

Structured `value` types use snake_case names and have static fields without IDs.
They are final, with no inheritance or extensions. Fields accept non-collection
values and entity references; references store the target ID. Recursive value
containment, collection fields, defaults and annotations other than title/description
are rejected. `attr` and `static attr` are equivalent in a value. Same-type equality
uses all fields, comparing referenced IDs; ordering is not defined by default.
The checker validates declarations and does not evaluate values.

Current implementation limits:

- A file has at most one namespace, before imports and declarations. Omitting it
  places declarations in the repository prefix namespace.
- Imports accept one name, without aliases or wildcards. Import collisions fail.
- Identifiers use ASCII letters, digits, and underscores. Strings use single
  quotes; `\'` represents a quote and `\\` a backslash. Other escape sequences
  retain their spelling. Formatting preserves the original string spelling and
  does not choose a regular-expression dialect.
- `snowflake_id` is a built-in value type. Its bit layout and generation policy
  are not specified. Unresolved types fail; value aliases are not defined.
- Decodes names are expected to be plural; the checker validates PascalCase but
  does not attempt linguistic plural detection.
- Static checks describe model declarations. They do not validate business data
  completeness, snapshot consistency, regex matches, or stored collection values.
- The language specifies `NOT_DEFINED` when a subtype field does not apply to an
  instance's actual type. Instance evaluation, SCD history storage, and configuration
  overrides are future runtime or backend work. The checker does not execute them;
  `NOT_DEFINED` is not currently accepted as a configuration default literal.

## Formatting

Formatting parses all selected sources before writing. It does not require
resolved references, so incomplete model designs can still be formatted. Selecting
a file formats only that file, even when it belongs to a repository.

Output uses UTF-8 without BOM, LF endings, four-space indentation, normalized
punctuation spacing, two spaces before inline comments, and one space after `#`.
It preserves string spelling, comment text, declaration order, and at most one
existing blank separator. Every file ends with exactly one LF character (`\n`),
so an editor shows one empty final line. Extra trailing blank lines are removed.
Empty files also normalize to a single LF. Input accepts LF or CRLF, with or without
a terminal newline. Standalone `---` lines delimit opaque Markdown blocks at the
top level or inside declarations. Their body whitespace is preserved, including
blank lines and trailing spaces; only CRLF is normalized. Unclosed blocks fail
parsing. Blocks do not bind to declarations or participate in name resolution.

Each changed file is written through a temporary file in the same directory and
atomically replaced. A syntax or decoding failure prevents all writes. An I/O
failure during publication stops the run; earlier replacements remain in place.
Avoid concurrent edits while formatting. Repeating a successful run changes nothing.

## Development

The parser retains a concrete syntax tree and source positions. The formatter
uses those positions to preserve comments; the validator resolves a compilation
unit by resolving scopes, merging entity fields, checking field relationships, and
validating types. The filesystem layer owns discovery and publication. Thin
lclang workflows expose the `check` and `format` commands.

Source and unit tests use the same functional package structure:

| Source package under `src/yambl_compiler/` | Matching tests | Responsibility |
| --- | --- | --- |
| `syntax/parser.py`, `syntax/grammar.lark` | `tests/syntax/test_parser.py` | Tokens, parse trees, source diagnostics |
| `semantics/validation.py` | `tests/semantics/test_validation.py` | Compilation-unit validation |
| `semantics/symbols.py` | `tests/semantics/test_symbols.py` | Namespace symbols and file-local imports |
| `semantics/types.py` | `tests/semantics/test_types.py` | Type constraints and external Decodes |
| `semantics/fields.py` | `tests/semantics/test_fields.py` | Metadata, modes, dimensions, and defaults |
| `semantics/signatures.py` | `tests/semantics/test_signatures.py` | Resolved field contract comparisons |
| `semantics/entities.py` | `tests/semantics/test_entities.py` | Merged attributes, locations, and inheritance |
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

## VS Code language server

The companion [YAMBL extension](https://github.com/gokurakujoudo/yambl/tree/feat/language-foundations/tools/vscode)
bundles a JavaScript LSP in `yambl/tools/lsp/`. It runs with VS Code's own Node
runtime and never invokes this Python package. The Python CLI remains available
for repository checks and automation.

When changing syntax, keywords, annotations, built-ins, or validation rules, update
the LSP and TextMate grammar in the language repository too. From this repository,
run `node ../yambl/tools/lsp/tests/parity.cjs` to compare syntax acceptance,
core diagnostics, and formatting against the editor regression corpus and FMC sources.
That development check uses this repository's venv; installed editor users do not
need Python. The server's implementation limits are documented with its source.

The editor intentionally suppresses `undefined Decodes` warnings because those
dictionaries may be supplied dynamically. This CLI continues to report each
external dictionary use. Missing explicit imports and unknown entity types remain
errors in both implementations. Parity tests compare the shared validation rules
before the editor applies that diagnostic filter.
