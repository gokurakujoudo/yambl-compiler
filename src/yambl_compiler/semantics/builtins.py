"""Read the model resources distributed with the compiler."""

from dataclasses import replace
from pathlib import Path

from yambl_compiler.syntax.parser import Diagnostic, Document, children, parse, tokens

MODEL_ROOT = Path(__file__).parents[1] / 'lang'
AUTHOR_ROOT = Path(__file__).parents[4] / 'yambl' / 'lang'


def load_models(root: Path = MODEL_ROOT) -> tuple[Document, ...]:
    """Parse supplied model files without registering user declarations.

    :param root: Model resource directory.
    :returns: Built-in source documents.
    :raises ValueError: If a model has invalid syntax or lacks namespace builtins.
    """
    result: list[Document] = []
    for path in sorted(root.rglob('*.yambl')):
        doc = parse(path.read_text(encoding='utf-8'), path)
        if isinstance(doc, Diagnostic):
            raise ValueError(str(doc))
        namespaces = children(doc.tree, 'namespace_decl')
        if not namespaces or ''.join(tokens(namespaces[0])[1:]) != 'builtins':
            raise ValueError(f'{path}: built-in models must declare namespace builtins')
        result.append(replace(doc, builtin=True))
    return tuple(result)


DOCUMENTS = load_models()
DECLARATIONS = {
    str(next(t for t in tokens(children(node, f'{node.data}_header')[0]) if t.type == 'NAME')): node
    for doc in DOCUMENTS
    for kind in ('entity', 'value', 'decodes', 'type_alias')
    for node in children(doc.tree, kind)
}
ENTITIES = {name for name, node in DECLARATIONS.items() if node.data == 'entity'}
STRUCTURES = {name for name, node in DECLARATIONS.items() if node.data == 'value'}
FINALS = {
    name
    for name, node in DECLARATIONS.items()
    if node.data == 'entity' and tokens(children(node, 'entity_header')[0])[0].type == 'FINAL'
}
