"""Parse line-oriented transaction documents with exact YAMBL literals."""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from yambl_compiler.transactions.model import (
    Change,
    Condition,
    Group,
    Image,
    ObjectValue,
    Operation,
    Plan,
    Symbol,
    Transaction,
    TransactionGroup,
    Value,
)

TOKEN = re.compile(
    r"(?P<space>[ \t]+)|(?P<newline>\n)|(?P<comment>\#[^\n]*)"
    r"|(?P<string>'(?:[^'\\\n]|\\[^\n])*')"
    r'|(?P<number>-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)'
    r'|(?P<name>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)'
    r'|(?P<punct>[{}\[\](),:=])'
)
KINDS: dict[str, Literal['insert', 'update', 'delete', 'replace']] = {
    'insert': 'insert',
    'update': 'update',
    'delete': 'delete',
    'replace': 'replace',
}


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


@dataclass(frozen=True)
class Token:
    """Preserve token spelling and coordinates for diagnostics and formatting.

    :param text: Original spelling.
    :param kind: Lexical category.
    :param line: One-based line.
    :param column: One-based column.
    """

    text: str
    kind: str
    line: int
    column: int


def tokenize(text: str) -> tuple[Token, ...]:
    """Scan a document without interpreting strings or losing comments.

    :param text: Source text; LF and CRLF are accepted.
    :returns: Tokens including comments and a final EOF token.
    :raises DocumentError: On an unrecognized character or unterminated string.
    """
    text = text.replace('\r\n', '\n')
    result: list[Token] = []
    offset, line, column = 0, 1, 1
    while offset < len(text):
        match = TOKEN.match(text, offset)
        if match is None:
            raise DocumentError('invalid token or unterminated string', line, column)
        spelling, kind = match.group(), match.lastgroup
        assert kind is not None
        if kind == 'newline':
            line, column = line + 1, 1
        else:
            if kind != 'space':
                result.append(Token(spelling, kind, line, column))
            column += len(spelling)
        offset = match.end()
    result.append(Token('', 'eof', line, column))
    return tuple(result)


class Parser:
    """Parse trusted grammar from an untrusted token stream."""

    def __init__(self, text: str) -> None:
        """Initialize a comment-free cursor.

        :param text: Source document.
        :returns: None.
        """
        self.tokens = tuple(t for t in tokenize(text) if t.kind != 'comment')
        self.index = 0
        self.depth = 0
        self.layout: dict[Token, int] = {}

    def line(self) -> None:
        """Mark the current token as a formatted line start.

        :returns: None.
        """
        self.layout[self.current] = self.depth

    def open_block(self) -> Token:
        """Open a structural block, distinct from a data object literal.

        :returns: Opening brace.
        """
        token = self.take('{')
        self.depth += 1
        self.line()
        return token

    def close_block(self) -> None:
        """Close a structural block and mark both line boundaries.

        :returns: None.
        """
        self.depth -= 1
        self.line()
        self.take('}')
        self.line()

    @property
    def current(self) -> Token:
        """Return the current token.

        :returns: Current token, including EOF.
        """
        return self.tokens[self.index]

    def fail(self, message: str) -> DocumentError:
        """Construct an error at the current token.

        :param message: Diagnostic message.
        :returns: Located exception.
        """
        return DocumentError(message, self.current.line, self.current.column)

    def take(self, spelling: str = '') -> Token:
        """Consume a token, optionally requiring its exact spelling.

        :param spelling: Expected spelling, or empty for any non-EOF token.
        :returns: Consumed token.
        :raises DocumentError: On a mismatch or premature EOF.
        """
        token = self.current
        if token.kind == 'eof' or (spelling and token.text != spelling):
            raise self.fail(f'expected {spelling or "token"}')
        self.index += 1
        return token

    def name(self, qualified: bool = False) -> str:
        """Consume an identifier.

        :param qualified: Whether dot-separated names are allowed.
        :returns: Identifier spelling.
        :raises DocumentError: On an invalid identifier.
        """
        token = self.current
        if token.kind != 'name' or (not qualified and '.' in token.text):
            raise self.fail('expected identifier')
        self.take()
        return token.text

    def string(self) -> str:
        """Decode YAMBL quotes and backslashes, preserving other escapes.

        :returns: String contents.
        :raises DocumentError: If the token is not a string.
        """
        if self.current.kind != 'string':
            raise self.fail('expected single-quoted string')
        return re.sub(r"\\(['\\])", r'\1', self.take().text[1:-1])

    def value(self, depth: int = 0) -> Value:
        """Parse a literal without evaluating expressions.

        :param depth: Container nesting depth.
        :returns: Exact literal value or symbol.
        :raises DocumentError: On invalid syntax or excessive nesting.
        """
        if depth > 64:
            raise self.fail('literal nesting exceeds 64')
        token = self.current
        if token.kind == 'string':
            return self.string()
        if token.kind == 'number':
            self.take()
            try:
                return Decimal(token.text) if re.search('[.eE]', token.text) else int(token.text)
            except (ValueError, ArithmeticError) as error:
                raise self.fail('invalid numeric literal') from error
        if token.kind == 'name':
            self.take()
            if token.text in ('true', 'false'):
                return token.text == 'true'
            return Symbol(token.text)
        if token.text == '[':
            self.take()
            items: list[Value] = []
            while self.current.text != ']':
                if items:
                    self.take(',')
                items.append(self.value(depth + 1))
            self.take(']')
            return tuple(items)
        if token.text == '{':
            self.take()
            pairs: list[tuple[Value, Value]] = []
            while self.current.text != '}':
                if pairs:
                    self.take(',')
                key = self.value(depth + 1)
                self.take(':')
                pairs.append((key, self.value(depth + 1)))
            self.take('}')
            return ObjectValue(tuple(pairs))
        raise self.fail('expected literal or model name')

    def columns(self) -> tuple[str, ...]:
        """Read nonempty unique column names.

        :returns: Ordered columns.
        :raises DocumentError: On empty or duplicate names.
        """
        self.take('(')
        result = [self.name()]
        while self.current.text == ',':
            self.take(',')
            result.append(self.name())
        self.take(')')
        if len(set(result)) != len(result):
            raise self.fail('duplicate column')
        return tuple(result)

    def rows(self, width: int) -> tuple[tuple[Value, ...], ...]:
        """Read one complete record per physical line.

        :param width: Expected number of values in every row.
        :returns: Literal records.
        :raises DocumentError: On row width, continuation or inline block errors.
        """
        opened = self.open_block()
        result: list[tuple[Value, ...]] = []
        previous_line = opened.line
        while self.current.text != '}':
            self.line()
            line = self.current.line
            if line <= previous_line:
                raise self.fail('each data record must occupy its own line')
            row = [self.value()]
            while self.current.text == ',' and self.current.line == line:
                self.take(',')
                row.append(self.value())
            if self.tokens[self.index - 1].line != line:
                raise self.fail('a data record cannot span multiple lines')
            if len(row) != width:
                raise self.fail(f'expected {width} values, received {len(row)}')
            if self.current.line == line:
                raise self.fail('unexpected token after data record')
            result.append(tuple(row))
            previous_line = line
        self.close_block()
        return tuple(result)

    def scope(self) -> tuple[Condition, ...]:
        """Read equality and half-open interval conditions.

        :returns: Nonempty unique coordinate conditions.
        :raises DocumentError: On invalid syntax or duplicate coordinates.
        """
        self.take('scope')
        self.take('(')
        result: list[Condition] = []
        while self.current.text != ')':
            if result:
                self.take(',')
            name = self.name()
            if name in {c.name for c in result}:
                raise self.fail(f'duplicate scope coordinate: {name}')
            end: Value | None = None
            if self.current.text == 'in':
                self.take('in')
                self.take('[')
                value = self.value()
                self.take(',')
                end = self.value()
                self.take(')')
            else:
                self.take('=')
                value = self.value()
            result.append(Condition(name, value, end))
        self.take(')')
        if not result:
            raise self.fail('scope must contain an owner coordinate')
        return tuple(result)

    def operation(self) -> Operation:
        """Read one entity batch or field operation.

        :returns: Syntactically valid operation.
        :raises DocumentError: On an invalid operation shape.
        """
        self.line()
        token = self.take()
        if token.text not in KINDS:
            raise self.fail('expected insert, update, delete or replace')
        kind = KINDS[token.text]
        target = self.name(True)
        owned = False
        if self.current.text == 'owned_data':
            if kind != 'delete':
                raise self.fail('owned_data is only valid for entity deletion')
            self.take('owned_data')
            self.take('delete_all')
            owned = True
        conditions: tuple[Condition, ...] = ()
        if self.current.text == 'scope':
            if kind not in ('replace', 'delete') or owned:
                raise self.fail('scope requires replace or field delete')
            conditions = self.scope()
            if kind == 'delete':
                return Operation(kind, target, (), (), conditions, line=token.line)
            self.take('columns')
        elif kind == 'replace':
            raise self.fail('replace requires scope')
        columns = self.columns()
        return Operation(
            kind, target, columns, self.rows(len(columns)), conditions, owned, token.line
        )

    def revision(self) -> int:
        """Require the supported document revision.

        :returns: Revision one.
        :raises DocumentError: On unsupported revisions.
        """
        self.line()
        self.take('version')
        if self.current.text != '1':
            raise self.fail('unsupported document version; expected 1')
        self.take('1')
        self.line()
        return 1

    def digest(self) -> str:
        """Read a lowercase SHA-256 string.

        :returns: Digest text.
        :raises DocumentError: On a malformed digest.
        """
        value = self.string()
        if not re.fullmatch('[0-9a-f]{64}', value):
            raise self.fail('expected lowercase SHA-256 digest')
        self.line()
        return value

    def plan(self) -> Plan:
        """Read a complete Plan block.

        :returns: Plan including all original data.
        :raises DocumentError: On invalid syntax or duplicate groups.
        """
        self.line()
        self.take('plan')
        name = self.name()
        self.open_block()
        version = self.revision()
        self.take('model')
        model = self.digest()
        groups: list[Group] = []
        while self.current.text != '}':
            self.take('group')
            group = self.name()
            if group in {g.name for g in groups}:
                raise self.fail(f'duplicate group: {group}')
            self.open_block()
            operations: list[Operation] = []
            while self.current.text != '}':
                operations.append(self.operation())
            self.close_block()
            groups.append(Group(group, tuple(operations)))
        self.close_block()
        return Plan(name, model, tuple(groups), version)

    def image(self) -> Image:
        """Read an image with business column names.

        :returns: Recorded data image.
        """
        target = self.name(True)
        columns = self.columns()
        return Image(target, columns, self.rows(len(columns)))

    def transaction(self) -> Transaction:
        """Read a transaction envelope and its business data images.

        :returns: Parsed transaction; integrity is checked by the public entry point.
        :raises DocumentError: On invalid metadata or group references.
        """
        self.line()
        self.take('transaction')
        id_ = self.string()
        self.open_block()
        version = self.revision()
        self.take('time')
        time = self.string()
        self.line()
        source = ''
        if self.current.text == 'source':
            self.take('source')
            source = self.string()
            self.line()
        try:
            if str(UUID(id_)) != id_ or (source and str(UUID(source)) != source):
                raise ValueError('noncanonical UUID')
            if not time.endswith('Z'):
                raise ValueError('timestamp must end in Z')
            datetime.fromisoformat(time)
        except ValueError as error:
            raise self.fail(f'invalid transaction metadata: {error}') from error
        self.take('digest')
        digest = self.digest()
        plan = self.plan()
        groups: list[TransactionGroup] = []
        while self.current.text != '}':
            self.take('group')
            name = self.name()
            if name not in {g.name for g in plan.groups} or name in {g.name for g in groups}:
                raise self.fail('unknown or duplicate transaction group')
            self.open_block()
            changes: list[Change] = []
            warnings: list[str] = []
            while self.current.text != '}':
                if self.current.text == 'warning':
                    self.line()
                    self.take('warning')
                    warnings.append(self.string())
                    self.line()
                    continue
                self.line()
                self.take('change')
                target = self.name(True)
                scope = self.scope()
                self.open_block()
                before: list[Image] = []
                after: list[Image] = []
                while self.current.text == 'before':
                    self.take('before')
                    before.append(self.image())
                while self.current.text == 'after':
                    self.take('after')
                    after.append(self.image())
                if not before and not after:
                    raise self.fail('change requires a before or after image')
                self.close_block()
                changes.append(Change(target, scope, tuple(before), tuple(after)))
            self.close_block()
            groups.append(TransactionGroup(name, tuple(changes), tuple(warnings)))
        self.close_block()
        return Transaction(id_, time, plan, tuple(groups), source, digest, version)


def parse(text: str) -> Plan | Transaction:
    """Read one Plan or Transaction and reject trailing content.

    :param text: Document source.
    :returns: Typed document.
    :raises DocumentError: On syntax, metadata or transaction integrity errors.
    """
    from yambl_compiler.transactions.writing import semantic_digest

    parser = Parser(text)
    result = parser.plan() if parser.current.text == 'plan' else parser.transaction()
    if parser.current.kind != 'eof':
        raise parser.fail('unexpected content after document')
    if isinstance(result, Transaction) and result.digest != semantic_digest(result):
        raise parser.fail('transaction digest mismatch')
    return result
