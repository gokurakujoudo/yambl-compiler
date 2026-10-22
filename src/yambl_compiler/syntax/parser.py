"""Parse source text and retain token positions for diagnostics and formatting."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from lark import Lark, Token, Tree, UnexpectedInput

PARSER = Lark(
    Path(__file__).with_name('grammar.lark').read_text(encoding='utf-8'),
    parser='lalr',
    propagate_positions=True,
    keep_all_tokens=True,
    maybe_placeholders=False,
)


class TextParser(Protocol):
    """Narrow Lark's custom-lexer API to this application's text-only parser."""

    def parse(self, text: str) -> Tree[Token]:
        """Parse text with positioned tokens and no transformer.

        :param text: Source text.
        :returns: Concrete parse tree.
        """
        ...


@dataclass(frozen=True)
class Diagnostic:
    """Locate a source problem.

    :param path: Source file name.
    :param line: One-based source line.
    :param column: One-based source column.
    :param message: Human-readable explanation.
    """

    path: Path
    line: int
    column: int
    message: str

    def __str__(self) -> str:
        """Render an editor-compatible diagnostic.

        :returns: Path, location, severity, and explanation.
        """
        return f'{self.path}:{self.line}:{self.column}: error: {self.message}'


@dataclass(frozen=True)
class Document:
    """Retain one parsed source.

    :param path: Source identity.
    :param text: Original source text.
    :param tree: Concrete syntax tree including punctuation.
    """

    path: Path
    text: str
    tree: Tree[Token]


def tokens(node: Tree[Token]) -> list[Token]:
    """Collect source tokens in order.

    :param node: Parsed subtree.
    :returns: Tokens including punctuation, excluding comments and whitespace.
    """
    result: list[Token] = []
    for child in node.children:
        if isinstance(child, Token):
            result.append(child)
        else:
            result.extend(tokens(child))
    return result


def children(node: Tree[Token], rule: str) -> list[Tree[Token]]:
    """Select direct child rules.

    :param node: Parent tree.
    :param rule: Grammar rule name.
    :returns: Matching children in source order.
    """
    return [child for child in node.children if isinstance(child, Tree) and child.data == rule]


def parse(text: str, path: Path = Path('<input>')) -> Document | Diagnostic:
    """Parse one file without requiring referenced files.

    :param text: YAMBL source text; comment spacing is unrestricted.
    :param path: File name used in diagnostics.
    :returns: Parsed document or the first syntax diagnostic.
    """
    try:
        # Lark's custom-lexer input is untyped; this parser only accepts text and returns trees.
        return Document(path, text, cast(TextParser, PARSER).parse(text))
    except UnexpectedInput as error:
        return Diagnostic(path, max(1, error.line), max(1, error.column), 'invalid YAMBL syntax')
