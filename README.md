# YAMBL compiler

Static checks, formatting, and JSON model compilation for [YAMBL](https://github.com/gokurakujoudo/yambl),
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

Exit status is 0 for success, 1 for invalid sources or output destinations, I/O failures, or formatting
drift, and 2 for CLI usage or unexpected runtime errors. Source diagnostics and
summaries go to stdout; lclang workflow logs go to stderr. A dry run that finds
formatting drift returns 1. Warnings alone return 0.

## JSON intermediate model

Compile a rooted repository to a standalone artifact, then validate it independently:

```powershell
.\venv\Scripts\yambl.exe compile -o path ..\fmc-data-model -o output .\build\model.yambl-ir.json
.\venv\Scripts\yambl.exe check-ir -o path .\build\model.yambl-ir.json
```

`compile` requires `yambl-root.lclcfg`. A member file or subdirectory selects the
whole repository. `output` is required and must end in `.json`; `--dryrun` completes
analysis and validation without creating output files or directories. Failed
analysis or publication preserves an existing artifact. `check-ir` is read-only.
The CLI adapts the public `check-ir` spelling to lclang's internal `check_ir` command;
both spellings are accepted, and help lists the internal name with its public alias.

The artifact embeds the selected built-in definitions, resolved declarations,
logical field identities, effective implementations, expanded aliases, dependent
expressions, bound decorators, flags, external dictionaries, and original source
spans. Paths are relative. Comments, Markdown blocks, and source text are omitted.
Unresolved quoted dictionaries remain explicit dependencies and warnings; they
do not prevent compilation. No external dictionary data is fetched.

```python
from pathlib import Path
from yambl_compiler.ir import compile_repository, load_ir, validate_ir, write_ir

model = compile_repository(Path('../fmc-data-model'))
assert validate_ir(model) == ()
write_ir(model, Path('build/model.yambl-ir.json'))
loaded = load_ir(Path('build/model.yambl-ir.json'))
assert loaded == model
```

`compile_repository` and `load_ir` return an immutable `ir.model.Model`.
`validate_ir` returns a tuple of diagnostic strings, empty on success. The other
functions raise `ValueError` for invalid input or models and `OSError` for filesystem
failures. `load_ir` uses embedded definitions and the supported checking algorithms;
it does not read YAMBL sources or installed language resources. Validation rejects
duplicate JSON keys, unsupported nodes or revisions, broken references, invalid
cycles and constraints, and inconsistent derived views, even if hashes were recomputed.

The current `format_revision` is `5`. Constant declarations and contextual literal
types are retained as structured records and checked on independent loading.
Compatibility is not promised until the
format is frozen; unsupported revisions require recompilation. Output is deterministic
compact UTF-8 JSON with LF and one final newline. Semantic SHA-256
digests exclude source positions and compiler version. Integer and decimal payloads
are strings to preserve precision. The full contract is documented in
[JSON IR](https://github.com/gokurakujoudo/yambl/blob/feat/language-foundations/lang/syntex/ir.md).

SQLite generation and typed direct reads are available as described below.
Computed fields execute Hierarchy selection, effective ConfigGroup overrides and explicit result recovery.

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
with `@override` when its type matches or exact narrows it to a compatible constant. Independent branches sharing an
ancestor must mark same-name attributes with `@overload` and use compatible types with matching modes and metadata.
Overload groups need at least two independent implementations. Types are compared after transparent aliases expand; config defaults may differ.
A `final entity` cannot be inherited or extended.
Extensions add attributes, not identifiers. Inheritance and extensions currently
target user-defined entity types; `Decodes`, `ConfigGroup`, and `Hierarchy` are final.

Both `code<Currencies>` and `code<'Currencies'>` look up a Decodes instance in the
current file's scope. An unresolved bare name is an `undefined Decodes: NAME` error.
An unresolved quoted name is allowed as an external dictionary and produces a CLI
warning with its path, line, and column. These rules also apply inside generics and
`@dimension`. Missing explicit imports and unknown entity types remain errors.

Codes declared in YAMBL use PascalCase: an ASCII uppercase initial followed by
letters or digits, including names such as `SomeValue` and `USD`. `@flags` marks a
Decodes whose explicit entry numbers must be distinct positive single-bit masks.
Decimal and `0b` binary notation are accepted. Each flags dictionary provides
`None = 0` and `All` (all declared flags) automatically; declaring either name
explicitly is an error. In an empty flags dictionary, All equals None.
`AttrCats.Static + AttrCats.Config` combines codes from the same flags dictionary by
bitwise union. `AttrCats.All - AttrCats.Snapshot` removes Snapshot. Repeating an
addition or removal has no further effect. Both operators have the same precedence
and evaluate left to right, after member access.
The combination keeps its dictionary's code type and can be passed to aliases,
`attrs`, `field_mapping`, or an `exact` constraint. `AttrCats.None` selects no fields.
The checker validates these expressions without executing model instances.

`@title('...')` and `@description('...')` attach metadata to entities, Decodes
declarations, values, fields, type aliases, or decorator declarations. `@dimension(prices)`
references a named dimension declared inside an entity or extend block. Its value is
string/code; hierarchy and snapshot fallback_days are optional. Old inline pair syntax
reports a migration diagnostic. `@temporal(as_of_date: date)` applies to snapshot/scd.
Computed fields infer their dates and dimensions from dependencies.
`config` attributes are time-independent and may declare a default with `= literal`.
Supported defaults are integer literals for `integer` and `snowflake_id`, numeric
literals for `float` and `decimal`, single-quoted strings for `string`, and
`true` / `false` for `bool`. Other default types and expressions are not implemented.

`Entity` is the abstract ultimate entity base. Each business root keeps its own field
identities; `attrs<AttrCats.Static, Entity>` does not collect every business field.
`type<T>` stores T or a subtype's qualified name. `self.kind` selects a field
descriptor; `self.kind::type` selects its declared type and `self.kind::value`
selects its instance value. Multi-level instance paths are supported. `alias<...>`
expresses equal values and type dependencies are checked for cycles.

`attrs<AttrCats.Static, Security>` selects qualified field identities across the
target's inherited, own, extended and descendant attributes. IDs and collection
fields are excluded. `mapping<attrs<...> as Attr, Attr::type>` associates each field
with its declared type. The bound key has no instance value. `field_mapping` is a
transparent alias defined in `lang/builtins/field_mapping.yambl`.
Mappings accept string-compatible or entity-reference keys and non-collection
values. Codes, IDs, type names and field identities are string-compatible in one
direction; a plain string cannot implicitly become a code or type value.

`type money = decimal[18, 4]` and parameterized definitions such as
`type selected<t: type<Entity>> = t` work in user repositories. Parameters use
positional arguments with lexical scope and explicit instance dependencies.
Aliases preserve their spelling during formatting. `exact<value>` is restricted
to override/overload contracts. An inherited singleton cannot change or widen.
Overloads retain branch constraints and expose their compatible common type.

The checker retains effective fields, overload identities and declaration locations.
Configuration groups are supplied as typed storage records. Generated computation programs evaluate their conditions and priorities; no general instance-construction syntax is added.

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
is the `yambl/lang` repository, with an empty prefix and `namespace builtins`.
The complete namespace `builtins` allows authoring the bundled model names in any
checkout. Duplicate declarations within that repository still fail. Other namespaces,
including `fmc.builtins`, cannot redeclare those names. Only bundled resources are
globally visible; local additions to `builtins` follow ordinary scope rules. Run
`./tools/sync-models.ps1` to refresh Python package resources from the adjacent
language repository; `-Source` accepts its lang directory in another checkout.
The compiler runs without that checkout. Tests compare the resources when it is present.

Structured `value` types use snake_case names and have static fields without IDs.
They are final, with no inheritance or extensions. Fields accept non-collection
values and entity references; references store the target ID. Recursive value
containment, collection fields, defaults and field relationship markers are rejected.
Values accept title/description and custom decorators. `attr` and `static attr` are equivalent in a value. Same-type equality
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
  are not specified. Unresolved types fail. Transparent type aliases are supported.
- Decodes names are expected to be plural; the checker validates PascalCase but
  does not attempt linguistic plural detection.
- Static checks describe model declarations. They do not validate business data
  completeness, snapshot consistency, regex matches, or stored collection values.
- The language defines seven result constants: `NOT_DEFINED`, `NOT_APPLICABLE`,
  `EMPTY`, `NO_ENTRY`, `NO_DATA`, `ENTITY_NOT_FOUND`, and `ERROR`.
  Their names and meanings come from `lang/builtins/result-constants.json`.
  The checker reserves these declaration names. Computations can propagate them
  or handle them explicitly with `recover`; physical fields and configuration
  defaults do not accept them as ordinary literals. Quoted spellings remain strings.
  `Partial[T]` carries a usable calculation payload and immutable issue details;
  it has no physical field encoding. SQLite and JSONL readers share SCD selection,
  effective configuration and calculation result handling.

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
| `semantics/expressions.py` | `tests/semantics/test_expressions.py` | Transparent aliases and dependent type expressions |
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

The IR contract remains experimental. SQLite generation and direct reads are available; SQLite and JSONL share the same read semantics.
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

The editor suppresses `undefined Decodes` warnings for quoted names that may be
supplied dynamically; unresolved bare names remain errors. This CLI reports each
external dictionary use. Missing explicit imports and unknown entity types remain
errors in both implementations. Parity tests compare the shared validation rules
before the editor applies that diagnostic filter.

## Decorator declarations

A decorator declares typed metadata and must state its allowed targets:

```text
@targets(DecoratorTargets.All)
decorator @note(text: string = 'Note', *source?: string)
```

Calls accept positional values, then named values such as `text='Name'`, followed
by caller-defined pairs such as `source: code<'PriceSources'>`. Each starred
parameter receives one pair. Optional pairs come last. Missing ordinary arguments
use their declared defaults. Unknown names, duplicate assignments, missing required
arguments and incompatible types are errors. Parameters cannot be collections.

Decorators use the existing namespace and import rules. They can annotate entities,
values, dictionaries, fields, type aliases and decorator declarations. A decorator
can appear once on a definition unless its declaration has `@allow_multiple`.
That built-in marker applies only to decorator declarations. The compiler retains
metadata and checks parameters without executing user-defined decorators.

Inside a field decorator, `self` captures the field definition. `self::type` selects
its declared type and `self::entity` selects its declaring entity. For
`Holding.security: Security`, these are Security and Holding respectively.
`type<self::entity>` accepts Holding or a subtype. Fields from extensions belong to
the extended entity; inherited fields retain their original declaration context.
A captured field has no runtime instance value. Ordinary entity-reference parameters
accept string IDs when the target has one identifiable ID field of a supported
string or snowflake type; instance existence and regex matching are not checked.

Built-in signatures are distributed in `lang/builtins/decorators.yambl`.
Every signature, including `targets` itself, explicitly declares `@targets`.
`DecoratorTargets` is a language-defined flags dictionary with Entity, Value,
Decodes, Type, Decorator, Id, EntityAttr and ValueAttr codes, plus generated None
and All. Missing scope declarations are errors even on unused decorators.
The checker matches calls against these declarations; built-in special behavior
remains in the compiler. `categories: code<AttrCats>` defaults to AttrCats.All and
filters only attributes. Value attributes are Static, including implicit `attr`.
For example, temporal declares EntityAttr with Snapshot + Scd categories.
Target expressions use the decorator declaration scope, and support flags union
and subtraction. Collection attributes follow the same target/category rules.
`@temporal(effective_date: date)` accepts a caller-chosen parameter name.
`@title(text='Name')` and `@title('Name')` are equivalent. Empty calls may omit
parentheses. Custom metadata participates in override/overload signatures after
argument binding and default substitution. Call order is ignored; repetition counts
and pair names/types are retained. Relationship markers remain outside signatures.

## SQLite, JSONL and typed reads

Compile a rooted repository, then generate into a new or empty directory:

```console
yambl compile -o path ../fmc-data-model -o output ./build/model.yambl-ir.json
yambl schema -o output ./build/yambl-ir.schema.json
yambl generate -o path ./build/model.yambl-ir.json -o output ./build/generated -o package fmc_model
```

The output contains `schema.sql`, a complete readable `schema.md`,
`storage-manifest.json`, and the `fmc_model` package with bundled IR and `py.typed`.
The SQL, reference and manifest share one physical model. Generation rejects
unsupported business fields before writing. `--dryrun` validates and renders
without creating output directories. Database migrations and production writes
are not provided.

Storage layout revision 5 uses names such as `tb_core_portfolio` and
`tb_core_security_price`. Ungrouped dimensionless static fields, including per-field states,
share the root identity table. Collection members use the field table name;
separate collection headers add `_status`. Readers reject databases with an older
layout revision. Regenerate disposable databases when updating the generated package.
Each root's `_config` table is keyed by `(id, field)`. Like sparse groups it uses
only the required string, bool, int, decimal and date lanes. Normal scalar rows
use exactly the matching lane; non-normal rows and collection headers use NULL
in every scalar lane. Collections retain their member tables.

`@sparse('bond_info')` moves a non-collection, dimensionless static field into
the root's `_bond_info` long table. It has `(id, field)` keys, a state column,
and the string, bool, int, decimal or date payload columns used by that group.
References use the target ID's column. CHECK constraints require exactly the
selected payload for VALUE and NULL in all payload columns for other states.
`@dense('bond_info')` instead uses a wide group with per-field state and payload
columns. Both decorators require a nonempty snake_case category and exclude
each other and `@dimension`. Conflicting group layouts or physical names fail
before output is written. Overrides and overloads share the logical field's
group; explicit placements must agree. Grouping does not change the read API.

The schema also includes Decodes, Hierarchy and ConfigGroup tables. Declared
dictionaries and their entries are initialized from the bundled IR, including
flags None/All entries. ConfigGroup mappings use child tables keyed by group ID
and logical field name, with a typed JSON value. SQL checks the JSON envelope;
the effective-config evaluator checks mapping types against the actual field implementation.
Generic public read methods for these mapping fields remain unavailable; the manifest
marks them with `readable: false`.

IR revision 5 uses compact JSON. The packaged Draft 2020-12 schema validates
structure offline; semantic validation also checks references and derived views.
Recompile revision 1 and 2 artifacts. Ordinary `attr` now means `static attr`, and
`AttrCats.Attr` has been removed in favor of `AttrCats.Static`.

Generated packages require only Python 3.14+ and the standard library. They carry
validated runtime contracts pinned by SHA-256; loading does not recompile the IR.
`open_sqlite(path)` opens a read-only transaction; `open_files(path)` opens a JSONL
snapshot. `open(path)` remains the SQLite alias. Config defaults still apply only
to NO_ENTRY and carry `origin='model_default'`.

Entities are frozen dataclasses with identity equality. Each attribute property
lazily reads all its data. Time or dimension fields return immutable tuples of
field-specific frozen records; each record contains coordinates and a ReadResult
payload. Scalar properties return ReadResult directly. No records means a
successful empty tuple. References remain explicit EntityRef values.

Methods cache normalized queries and reuse complete property caches. Cached data
survives close; new reads return ERROR. Errors are never cached. Range lists are
copied before returning. Computed methods execute named-dimension selection and effective per-instance configuration.

The language repository's `lang/syntex/storage.md` documents storage rules and
tracks implemented and pending capabilities. The FMC repository includes an
initializer and seed data for a disposable example database.

Generated entity classes include their declared title and description when present.
Attribute read methods document the YAMBL type, dimensions and every entity that
declares the logical field, sorted by qualified name. Override and overload owners
are included; an extension belongs to its target entity. Inheritance alone does
not add another declaration owner. Optional Hierarchy selectors are identified as
metadata rather than Python arguments.

Every generated method has rST parameter and return fields, including result types,
date boundaries, batch ordering and config default behavior. Model prose is kept
in literal blocks and quoted safely in Python. Generated docstrings are tested as
rST and checked against the actual Python signatures.

The output also includes `schema-relations.puml`, `python-schema.md`, and a
`repository/` JSONL template seeded with built-in dictionaries. The PlantUML graph
lists every table and column with its SQL type, nullability and primary-key
position. Arrows connect specific columns: solid arrows are enforced foreign keys;
dashed arrows are logical ownership or entity references. Repeated FK labels group
the columns of a composite constraint. Reference labels identify the logical field
and any discriminator used by shared payload columns. `schema.md` embeds the same
PlantUML source; displaying it as a diagram requires a PlantUML renderer. All these
outputs use the same physical model.

JSONL repositories contain `repository.json` and `tables/<table>.jsonl` for every
table. Metadata pins format revision 1, layout revision 5, the model digest and
each file's SHA-256. Tables are loaded and indexed on first use. Every new backend
read checks file identity, size and timestamps; table loads also verify hashes
and check file state before and after reading. Once changes are detected, reopen
the reader. Existing immutable field caches remain usable. Rows require complete
columns, canonical scalar encodings, unique keys and valid state/payload lanes.

`reader.validate()` explicitly scans data and checks same-field reference cycles.
It returns an immutable ValidationReport with `valid` and `issues`. Cycles are
scoped by inheritance root, logical field, dimension tuple and snapshot date or
intersecting SCD period. Cross-field and cross-root references are not merged;
dangling references remain allowed. Each cyclic component has one deterministic
witness. `python -m fmc_model check-data --backend sqlite|files --path PATH` returns
0 for valid data, 1 for violations or read failure, and 2 for invalid arguments.

Keep the latest full generation under `build/latest/`. The FMC initializer accepts
`--backend files` for JSONL and validates either backend after seeding. Generation
still refuses nonempty destinations and does not provide a production writer.

## Computed fields and batched reads

`computed attr name: Type { let local = expression return expression }` declares a
read-only calculation. Its inferred inputs, defaults and dependency identities are
stored in IR revision 5. Runtime contract 4 and storage layout 5 require regenerated
artifacts; JSONL format remains 1. Computed fields add no physical columns.

The generated package exposes frozen request classes and `read_*_many` methods.
Requests retain input order and duplicates while dependency reads are batched.
SQLite filters identities and date windows in SQL; Python performs exact decimal
arithmetic. JSONL tables load once per session and reuse key/date indexes.

The result union includes `Partial[T]`. Strict mode preserves failures; outer
`best_effort=True` may omit non-error contributions while retaining their issues.
`recover` is explicit and lazy. ConfigGroup priority overrides instance config,
which overrides the actual subtype's model default. Cached field data can serve
calculations after close when no new backend read is needed.

See the [calculation contract](../yambl/lang/syntex/computations.md) and the
[capability matrix](../yambl/lang/syntex/storage.md#能力清单). General functions,
write APIs, migrations, division, FX conversion and automatic combinations of
multiple Hierarchies are outside this release.

Optional calculation inputs can be declared with
`@extra_input_params(day: date = '2026-01-01', 'Observation date')`.
Caller values override declaration defaults. `input.day ?? input.as_of_date` uses
its right operand only when the input is omitted; `.default(...)` is no longer
accepted. Native non-normal results still require `recover`.
Decorator calls accept optional trailing description metadata without adding a
formal parameter to each declaration. IR revision 5 preserves pair defaults;
runtime contract 4 carries the same checked metadata into generated packages.
