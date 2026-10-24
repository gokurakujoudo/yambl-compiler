"""Validate dictionary codes and independent flag bits."""

import re
from decimal import Decimal

from lark import Token, Tree

from yambl_compiler.semantics.symbols import Unit
from yambl_compiler.syntax.parser import Diagnostic, annotations, children, diagnostic, tokens


def is_flags(declaration: Tree[Token]) -> bool:
    """Identify a dictionary with flags semantics.

    :param declaration: Dictionary declaration.
    :returns: Whether the flags marker is present.
    """
    return bool(annotations(declaration, 'flags'))


def check_decodes(node: Tree[Token], unit: Unit, diagnostics: list[Diagnostic]) -> None:
    """Check code spelling, uniqueness and explicit flag masks.

    :param node: Dictionary declaration.
    :param unit: Original source scope.
    :param diagnostics: Destination for located errors.
    :returns: None.
    """
    markers = annotations(node, 'flags')
    if len(markers) > 1:
        diagnostics.append(diagnostic(unit.document, markers[-1], 'duplicate flags annotation'))
    codes: set[str] = set()
    masks: set[int] = set()
    for entry in children(node, 'entry'):
        order, code = map(str, tokens(entry)[1:3])
        if not re.fullmatch(r'[A-Z][A-Za-z0-9]*', code):
            diagnostics.append(
                diagnostic(unit.document, entry, 'Decodes codes must use PascalCase')
            )
        if code in codes:
            diagnostics.append(
                diagnostic(unit.document, entry, f'duplicate code in Decodes: {code}')
            )
        codes.add(code)
        if markers:
            if code in ('None', 'All'):
                diagnostics.append(
                    diagnostic(
                        unit.document, entry, f'flags code {code} is generated automatically'
                    )
                )
            mask = int(order, 2) if '0b' in order else int(Decimal(order))
            if mask <= 0 or mask & (mask - 1):
                diagnostics.append(
                    diagnostic(
                        unit.document, entry, 'flags entries require a positive single-bit mask'
                    )
                )
            if mask in masks:
                diagnostics.append(diagnostic(unit.document, entry, 'duplicate flags mask'))
            masks.add(mask)
