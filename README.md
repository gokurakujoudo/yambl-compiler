# YAMBL compiler

A Python parser and compiler for
[YAMBL (Yet Another Model-Based Language)](https://github.com/gokurakujoudo/yambl).

This repository currently contains the project documentation and Git ignore
rules. The parser, compiler, and command-line interface are not implemented yet.

## Planned capabilities

- Check YAMBL syntax and report source diagnostics.
- Format YAMBL source files.
- Parse and compile a YAMBL file or repository into an intermediate representation.

Later versions will support further optimization and compilation of the
intermediate representation into targets such as database schemas, data
interfaces, and Python class libraries. The intermediate format and target
generation APIs are still to be defined.

## Local Python environment

An existing virtual environment is available at `venv/` in this workspace and
is excluded from Git. From this repository's root in PowerShell, check its
interpreter with:

```powershell
.\venv\Scripts\python.exe --version
```

The local environment uses Python 3.14.2. Supported Python versions and package
dependencies will be specified when implementation begins. A fresh clone does
not include the local virtual environment.

## Related projects

- [yambl](https://github.com/gokurakujoudo/yambl) defines the language.
- [fmc-data-model](https://github.com/gokurakujoudo/fmc-data-model) will contain a
  fund management company's data model written in YAMBL.
