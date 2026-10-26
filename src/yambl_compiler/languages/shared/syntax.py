"""Read shared data-language syntax from positioned Lark trees."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

from lark import Lark, Tree, UnexpectedInput
from lark import Token as LarkToken

from .model import Condition, ObjectValue, Symbol, Value

GRAMMARS = Path(__file__).parents[2] / 'grammars'
PARSERS = {
    language: Lark(
        (GRAMMARS / (language + '.lark')).read_text(encoding='utf-8'),
        import_paths=[str(GRAMMARS)],
        parser='lalr',
        propagate_positions=True,
        keep_all_tokens=True,
        maybe_placeholders=False,
    )
    for language in ('yaplan', 'yatran')
}


class TextParser(Protocol):
    """Narrow Lark's configurable parser to its text input contract."""

    def parse(self, text: str) -> Tree[LarkToken]:
        """Parse text with source positions.

        :param text: Complete source.
        :returns: Concrete syntax tree.
        """
        ...

    def lex(self, text: str, dont_ignore: bool = False) -> Iterator[LarkToken]:
        """Scan text with optional ignored terminals.

        :param text: Source text.
        :param dont_ignore: Whether comments and whitespace are returned.
        :returns: Positioned terminal iterator.
        """
        ...


class DocumentError(ValueError):
    """Report a document error with one-based source coordinates."""

    def __init__(self, message: str, line: int, column: int) -> None:
        """Retain source coordinates and a printable diagnostic.

        :param message: Error description.
        :param line: One-based line.
        :param column: One-based column.
        :returns: None.
        """
        super().__init__(f'{line}:{column}: {message}')
        self.message, self.line, self.column = message, line, column


def kind(node: Tree[LarkToken]) -> str:
    """Identify a rule imported through any language grammar.

    :param node: Concrete subtree.
    :returns: Rule name without Lark's import namespace.
    """
    return str(node.data).rsplit('__', 1)[-1]


def children(node: Tree[LarkToken], name: str = '') -> list[Tree[LarkToken]]:
    """Select direct subrules in source order.

    :param node: Parent rule.
    :param name: Rule name, or empty to select all rules.
    :returns: Matching child trees.
    """
    return [c for c in node.children if isinstance(c, Tree) and (not name or kind(c) == name)]


def tokens(node: Tree[LarkToken]) -> list[LarkToken]:
    """Collect positioned tokens in source order.

    :param node: Concrete subtree.
    :returns: Tokens including punctuation.
    """
    result: list[LarkToken] = []
    pending = list(reversed(node.children))
    while pending:
        child = pending.pop()
        if isinstance(child, LarkToken):
            result.append(child)
        else:
            pending.extend(reversed(child.children))
    return result


def fail(node: Tree[LarkToken], message: str) -> DocumentError:
    """Locate an error at a rule's first token.

    :param node: Invalid rule.
    :param message: Explanation.
    :returns: Located exception.
    """
    token = tokens(node)[0]
    return DocumentError(message, token.line or 1, token.column or 1)


def tree(text: str, language: str) -> Tree[LarkToken]:
    """Parse one complete document using the requested Lark grammar.

    :param text: Source with LF or CRLF line endings.
    :param language: yaplan or yatran.
    :returns: Document subtree.
    :raises DocumentError: On invalid grammar.
    """
    try:
        result = cast(TextParser, PARSERS[language]).parse(text.replace('\r\n', '\n'))
    except UnexpectedInput as error:
        raise DocumentError('invalid data document syntax', error.line, error.column) from error
    return children(result)[0]


def string(token: LarkToken) -> str:
    """Decode the shared YAMBL string escaping rules.

    :param token: Single-quoted string token.
    :returns: Text with quote and backslash escapes decoded.
    """
    return re.sub(r"\\(['\\])", r'\1', str(token)[1:-1])


def value(node: Tree[LarkToken], depth: int = 0) -> Value:
    """Decode a literal without evaluating expressions.

    :param node: Literal subtree.
    :param depth: Current container nesting depth.
    :returns: Exact typed value.
    :raises DocumentError: On numeric representation or nesting limits.
    """
    if depth > 64:
        raise fail(node, 'literal nesting exceeds 64')
    name = kind(node)
    if name == 'string':
        return string(tokens(node)[0])
    if name == 'number':
        spelling = str(tokens(node)[0])
        try:
            return Decimal(spelling) if re.search('[.eE]', spelling) else int(spelling)
        except (ValueError, ArithmeticError) as error:
            raise fail(node, 'invalid numeric literal') from error
    if name == 'symbol':
        spelling = str(tokens(node)[0])
        return spelling == 'true' if spelling in ('true', 'false') else Symbol(spelling)
    if name == 'sequence':
        return tuple(value(item, depth + 1) for item in children(node))
    return ObjectValue(
        tuple(
            (value(children(pair)[0], depth + 1), value(children(pair)[1], depth + 1))
            for pair in children(node)
        )
    )


def columns(node: Tree[LarkToken]) -> tuple[str, ...]:
    """Decode unique record column names.

    :param node: Column list.
    :returns: Ordered identifiers.
    :raises DocumentError: On duplicate columns.
    """
    result = tuple(str(t) for t in tokens(node)[1:-1] if str(t) != ',')
    if len(set(result)) != len(result):
        raise fail(node, 'duplicate column')
    return result


def rows(node: Tree[LarkToken], width: int) -> tuple[tuple[Value, ...], ...]:
    """Require each complete record on its own physical line.

    :param node: Data block.
    :param width: Column count.
    :returns: Exact records in source order.
    :raises DocumentError: On record shape or line boundary violations.
    """
    block_tokens = tokens(node)
    previous = block_tokens[0].line
    result: list[tuple[Value, ...]] = []
    records = children(node, 'row')
    for record in records:
        record_tokens = tokens(record)
        line = record_tokens[0].line
        if line == previous:
            raise fail(record, 'each data record must occupy its own line')
        if record_tokens[-1].line != line:
            raise fail(record, 'a data record cannot span multiple lines')
        row = tuple(value(item) for item in children(record))
        if len(row) != width:
            raise fail(record, f'expected {width} values, received {len(row)}')
        result.append(row)
        previous = line
    if records and block_tokens[-1].line == previous:
        raise fail(node, 'unexpected token after data record')
    return tuple(result)


def scope(node: Tree[LarkToken]) -> tuple[Condition, ...]:
    """Decode unique equality or half-open interval coordinates.

    :param node: Scope rule.
    :returns: Conditions in source order.
    :raises DocumentError: On missing or repeated coordinates.
    """
    result: list[Condition] = []
    for condition in children(node):
        name = str(tokens(condition)[0])
        if name in {c.name for c in result}:
            raise fail(condition, f'duplicate scope coordinate: {name}')
        values = children(condition)
        result.append(
            Condition(name, value(values[0]), value(values[1]) if len(values) == 2 else None)
        )
    if not result:
        raise fail(node, 'scope must contain an owner coordinate')
    return tuple(result)


def revision(node: Tree[LarkToken]) -> int:
    """Require document revision one.

    :param node: Revision rule.
    :returns: Supported revision.
    :raises DocumentError: On an unsupported revision.
    """
    if str(tokens(node)[1]) != '1':
        raise fail(node, 'unsupported document version; expected 1')
    return 1


def digest(node: Tree[LarkToken]) -> str:
    """Decode a lowercase SHA-256 string.

    :param node: Model or transaction digest rule.
    :returns: Digest contents.
    :raises DocumentError: On a malformed digest.
    """
    result = string(tokens(node)[1])
    if not re.fullmatch('[0-9a-f]{64}', result):
        raise fail(node, 'expected lowercase SHA-256 digest')
    return result


@dataclass(frozen=True)
class Token:
    """Keep spelling and position independently of grammar import namespaces.

    :param text: Original spelling.
    :param kind: Content, comment or EOF.
    :param line: One-based line.
    :param column: One-based column.
    """

    text: str
    kind: str
    line: int
    column: int


def positioned(token: LarkToken) -> Token:
    """Convert a concrete token for formatting.

    :param token: Lark token.
    :returns: Stable positioned spelling.
    """
    return Token(
        str(token),
        'comment' if str(token).startswith('#') else 'content',
        token.line or 1,
        token.column or 1,
    )


def tokenize(text: str) -> tuple[Token, ...]:
    """Scan data grammar terminals while preserving comments.

    :param text: Source document.
    :returns: Positioned tokens and EOF, without whitespace.
    :raises DocumentError: On an invalid character or string.
    """
    text = text.replace('\r\n', '\n')
    try:
        result = [
            positioned(t)
            for t in cast(TextParser, PARSERS['yatran']).lex(text, dont_ignore=True)
            if str(t.type) != 'WS'
        ]
    except UnexpectedInput as error:
        raise DocumentError(
            'invalid token or unterminated string', error.line, error.column
        ) from error
    return (*result, Token('', 'eof', text.count('\n') + 1, len(text.rsplit('\n', 1)[-1]) + 1))


def layout(node: Tree[LarkToken], depth: int = 0) -> dict[Token, int]:
    """Derive structural line starts from the grammar tree.

    :param node: Complete or nested data rule.
    :param depth: Current indentation level.
    :returns: Token-to-indentation mapping for structural boundaries.
    """
    name = kind(node)
    blocks = {'plan', 'transaction', 'group', 'transaction_group', 'change', 'rows'}
    lines = {
        'operation',
        'revision',
        'model_digest',
        'time',
        'source',
        'digest',
        'warning',
        'before',
        'after',
        'row',
    }
    result: dict[Token, int] = {}
    if name not in blocks | lines:
        return result
    leaves = tokens(node)
    if name != 'rows':
        result[positioned(leaves[0])] = depth
    if name in blocks:
        result[positioned(leaves[-1])] = depth
        depth += 1
    for child in children(node):
        result.update(layout(child, depth))
    return result
