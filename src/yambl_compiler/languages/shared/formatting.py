"""Format transaction documents without rewriting data or dropping comments."""

from .documents import document_tree, parse
from .syntax import Token, layout, tokenize


def join_tokens(tokens: list[Token]) -> str:
    """Apply punctuation spacing while preserving every literal token.

    :param tokens: One formatted line without comments.
    :returns: Line contents without indentation.
    """
    text = ''
    previous = ''
    for token in tokens:
        spelling = token.text
        space = bool(text)
        if spelling in (',', ':', ')', ']', '=') or previous in ('(', '[', '='):
            space = False
        if spelling == '}' and previous == '{':
            space = False
        text += (' ' if space else '') + spelling
        previous = spelling
    return text


def format_document(text: str) -> str:
    """Validate then format one document, including its existing comments.

    :param text: Plan or Transaction source.
    :returns: Four-space LF text with one final newline.
    :raises ValueError: On invalid syntax or transaction integrity.
    """
    expected = parse(text)
    boundaries = layout(document_tree(text))
    stream = tokenize(text)
    syntax_tokens = tuple(t for t in stream if t.kind != 'comment')
    output: list[str] = []
    pending: list[Token] = []
    depth, base, previous_line = 0, 0, 0

    def flush(comment: str | None = None) -> None:
        """Publish the pending line and an optional trailing comment.

        :param comment: Comment contents without its marker.
        :returns: None.
        """
        if pending or comment is not None:
            body = join_tokens(pending)
            if comment is not None:
                body += ('  ' if body else '') + '#' + (' ' + comment if comment else '')
            output.append('    ' * depth + body)
            pending.clear()

    for token in stream[:-1]:
        if token.kind == 'comment':
            if pending and token.line != pending[-1].line:
                flush()
            if not pending:
                # A standalone comment belongs to the next structural level.
                next_token = next(
                    (t for t in syntax_tokens if t.line > token.line), syntax_tokens[-1]
                )
                depth = boundaries.get(next_token, base)
                if next_token.text == '}':
                    depth += 1
            if previous_line and token.line > previous_line + 1 and output and output[-1]:
                output.append('')
            flush(token.text[1:].lstrip(' '))
            previous_line = token.line
            continue
        boundary = token in boundaries
        newline = bool(pending and token.line != pending[-1].line)
        if boundary or newline:
            flush()
            if previous_line and token.line > previous_line + 1 and output and output[-1]:
                output.append('')
            if boundary:
                base = boundaries[token]
                depth = base
            else:
                depth = base + 1
        pending.append(token)
        previous_line = token.line
    flush()
    result = '\n'.join(output).rstrip('\n') + '\n'
    if parse(result) != expected:
        raise ValueError('formatting changed document semantics')
    return result
