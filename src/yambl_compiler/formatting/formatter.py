"""Format parsed syntax while preserving comments and literal spelling."""

import re
from dataclasses import dataclass
from typing import cast

from lark import Token, Tree

from yambl_compiler.syntax.parser import Document, annotations, children, tokens


@dataclass(frozen=True)
class Piece:
    """Describe one output line or comment.

    :param start: Source character offset.
    :param end: Exclusive source end offset.
    :param indent: Indentation level.
    :param text: Canonical line contents.
    :param comment: Whether this piece is a comment.
    """

    start: int
    end: int
    indent: int
    text: str
    comment: bool = False


def line_piece(items: list[Token], indent: int) -> Piece:
    """Lay out one nonempty sequence of positioned parser tokens.

    :param items: Tokens from a syntactic line.
    :param indent: Block indentation level.
    :returns: Canonical line retaining its original source span.
    """
    result = ''
    previous = ''
    prior_equals = False
    keyword_equals = {
        items[i + 1].start_pos
        for i, item in enumerate(items[:-1])
        if item.type == 'NAME' and str(items[i + 1]) == '=' and str(items[0]) == '@'
    }
    for item in items:
        value = str(item)
        space = (
            result
            and value not in ('?', '.', '::', ':', ',', ')', '>', ']', '(', '[', '<')
            and previous not in ('*', '.', '::', '@', '(', '<', '[')
        )
        if item.start_pos in keyword_equals:
            space = False
        if previous == '=' and prior_equals:
            space = False
        prior_equals = item.start_pos in keyword_equals
        result += (' ' if space else '') + value
        previous = value
    # Lark assigns positions to every token because propagate_positions is enabled.
    return Piece(cast(int, items[0].start_pos), cast(int, items[-1].end_pos), indent, result)


def format_document(document: Document) -> str:
    """Normalize layout, preserving at most one existing blank separator.

    :param document: Successfully parsed source.
    :returns: UTF-8-ready text with LF line endings and exactly one final LF.
    """
    pieces: list[Piece] = []
    blocks: list[tuple[int, int]] = []
    # The start and field rules contain only subtrees, as specified by grammar.lark.
    for declaration in cast(list[Tree[Token]], document.tree.children):
        items = tokens(declaration)
        if declaration.data in ('entity', 'decodes', 'extension', 'value'):
            for metadata in annotations(declaration):
                pieces.append(line_piece(tokens(metadata), 0))
            header_tokens = tokens(children(declaration, f'{declaration.data}_header')[0])
            brace = next(item for item in items if item.type == 'LBRACE')
            header = line_piece([*header_tokens, brace], 0)
            closing = line_piece(items[-1:], 0)
            pieces.extend((header, closing))
            blocks.append((header.end, closing.start))
            for field in children(declaration, 'field'):
                for part in cast(list[Tree[Token]], field.children):
                    pieces.append(line_piece(tokens(part), 1))
            for entry in children(declaration, 'entry'):
                pieces.append(line_piece(tokens(entry), 1))
        else:
            annotation_offsets: set[int | None] = set()
            for call in annotations(declaration):
                call_tokens = tokens(call)
                pieces.append(line_piece(call_tokens, 0))
                annotation_offsets.update(token.start_pos for token in call_tokens)
            items = [item for item in items if item.start_pos not in annotation_offsets]
            pieces.append(line_piece(items, 0))
    masked = document.text
    for start, end in reversed(document.blocks):
        pieces.append(Piece(start, end, 0, document.text[start:end].replace('\r\n', '\n')))
        masked = masked[:start] + ' ' * (end - start) + masked[end:]
    for match in re.finditer(r"'([^'\\\r\n]|\\[^\r\n])*'|#[^\r\n]*", masked):
        if match.group().startswith('#'):
            indent = int(any(start <= match.start() < end for start, end in blocks))
            pieces.append(
                Piece(match.start(), match.end(), indent, '# ' + match.group()[1:].strip(), True)
            )
    lines: list[str] = []
    end = 0
    for piece in sorted(pieces, key=lambda item: item.start):
        gap = document.text[end : piece.start].replace('\r\n', '\n').replace('\r', '\n')
        if piece.comment and lines and '\n' not in gap:
            lines[-1] += '  ' + piece.text
        else:
            if lines and gap.count('\n') > 1:
                lines.append('')
            lines.append('    ' * piece.indent + piece.text)
        end = piece.end
    return '\n'.join(lines).rstrip() + '\n'
