"""Compile repositories and read or atomically publish standalone IR artifacts."""

import os
import tempfile
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from yambl_compiler.ir.codec import dumps, loads
from yambl_compiler.ir.model import Model
from yambl_compiler.ir.validation import validate_ir


def load_ir(path: Path) -> Model:
    """Read and fully validate an artifact independently of its source repository.

    :param path: JSON artifact path.
    :returns: Validated typed model.
    :raises ValueError: If JSON or embedded semantics are invalid.
    :raises OSError: If the file cannot be read.
    """
    model = loads(path.read_text(encoding='utf-8'))
    errors = validate_ir(model)
    if errors:
        raise ValueError('\n'.join(errors))
    return model


def compile_repository(path: Path) -> Model:
    """Analyze a whole rooted repository and lower all supported language constructs.

    :param path: Repository root, member directory or member source file.
    :returns: Complete validated intermediate model.
    :raises ValueError: If discovery, syntax, semantics or IR validation fails.
    :raises OSError: If sources cannot be read.
    """
    from yambl_compiler.application.project import source_files
    from yambl_compiler.application.repository import ROOT_NAME, find_root, read_prefix
    from yambl_compiler.ir.lowering import Lowering
    from yambl_compiler.semantics.builtins import AUTHOR_ROOT, MODEL_ROOT
    from yambl_compiler.semantics.symbols import namespace
    from yambl_compiler.semantics.validation import analyze
    from yambl_compiler.syntax.parser import Diagnostic, Document, parse

    root = find_root(path.resolve())
    if root is None:
        raise ValueError('compile requires a repository with yambl-root.lclcfg')
    prefix = read_prefix(root / ROOT_NAME)
    builtin = root in (AUTHOR_ROOT.resolve(), MODEL_ROOT.resolve())
    if builtin and prefix:
        raise ValueError('built-in prefix must be empty')
    documents: list[Document] = []
    for file in source_files(path, True):
        doc = parse(file.read_text(encoding='utf-8-sig'), file)
        if isinstance(doc, Diagnostic):
            raise ValueError(str(doc))
        if builtin and namespace(doc) != 'builtins':
            raise ValueError(f'{file}: built-in models must declare namespace builtins')
        documents.append(
            replace(doc, prefix=prefix, repository='' if builtin else str(root), builtin=builtin)
        )
    analysis = analyze(documents)
    errors = [str(d) for d in analysis.diagnostics if d.severity == 'error']
    if errors:
        raise ValueError('\n'.join(errors))
    model = Lowering(analysis, root, prefix, version('yambl-compiler')).model()
    errors = list(validate_ir(model))
    if errors:
        raise ValueError('\n'.join(errors))
    return model


def write_ir(model: Model, path: Path) -> None:
    """Publish one fully validated JSON file without exposing partial content.

    :param model: Complete intermediate model.
    :param path: Destination ending in .json; parent directories are created.
    :returns: None after atomic publication.
    :raises ValueError: If the model or destination suffix is invalid.
    :raises OSError: If publication fails; an existing destination is preserved before replacement.
    """
    if path.suffix.lower() != '.json':
        raise ValueError('IR output must use the .json extension')
    errors = validate_ir(model)
    if errors:
        raise ValueError('\n'.join(errors))
    text = dumps(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.yambl-ir-', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(text, encoding='utf-8', newline='\n')
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
