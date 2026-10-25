"""Translate positioned calculation syntax into immutable expression records."""

from typing import Literal, cast

from lark import Token, Tree

from yambl_compiler.computation.records import (
    Argument,
    Binding,
    Body,
    Case,
    Expression,
    Span,
)
from yambl_compiler.syntax.parser import children, string_value, tokens


def span(node: Tree[Token]) -> Span:
    """Read a complete source span from parser tokens.

    :param node: Nonempty positioned syntax.
    :returns: Character offsets and line/column coordinates.
    """
    first, last = tokens(node)[0], tokens(node)[-1]
    return Span(
        cast(int, first.start_pos),
        cast(int, last.end_pos),
        cast(int, first.line),
        cast(int, first.column),
        cast(int, last.end_line),
        cast(int, last.end_column),
    )


def branches(node: Tree[Token]) -> list[Tree[Token]]:
    """Select direct syntax children without punctuation.

    :param node: Syntax parent.
    :returns: Child rules in source order.
    """
    return [item for item in node.children if isinstance(item, Tree)]


def expression(node: Tree[Token]) -> Expression:
    """Lower a calculation without performing name or type resolution.

    :param node: Calculation syntax root.
    :returns: Explicit typed syntax with lazy branches retained.
    """
    location = span(node)
    parts = branches(node)
    items = tokens(node)
    kind = str(node.data)
    if kind == "calculation_name":
        return Expression("name", str(items[0]), (), (), (), location)
    if kind == "calculation_literal":
        token = items[0]
        if token.type == "STRING":
            return Expression("string", string_value(token), (), (), (), location)
        literal_kind: Literal["bool", "decimal", "integer"] = (
            "bool"
            if token.type == "BOOLEAN"
            else "decimal" if any(c in str(token) for c in ".eE") else "integer"
        )
        return Expression(literal_kind, str(token), (), (), (), location)
    if kind == "calculation_parentheses":
        return expression(parts[0])
    if kind == "lambda_expression":
        return Expression("lambda", str(items[0]), (expression(parts[0]),), (), (), location)
    if kind == "conditional_expression":
        return Expression("conditional", "", tuple(expression(p) for p in parts), (), (), location)
    if kind == "unary_expression":
        return Expression("unary", str(items[0]), (expression(parts[0]),), (), (), location)
    if kind == "calculation_postfix":
        result = expression(parts[0])
        for suffix in parts[1:]:
            members = tokens(suffix)
            member = str(members[1])
            member_kind: Literal["descriptor", "member"] = (
                "descriptor" if str(members[0]) == "::" else "member"
            )
            selected = Expression(member_kind, member, (result,), (), (), span(suffix))
            calls = children(suffix, "calculation_arguments")
            if not calls:
                result = selected
                continue
            arguments: list[Argument] = []
            cases: list[Case] = []
            for argument in branches(calls[0]):
                if argument.data == "recovery_case":
                    head = [str(t) for t in argument.children if isinstance(t, Token)]
                    marker = head.index("as") if "as" in head else head.index("->")
                    statuses = tuple(t for t in head[1:marker] if t != ",")
                    binding = head[marker + 1] if head[marker] == "as" else ""
                    cases.append(
                        Case(
                            statuses,
                            binding,
                            expression(branches(argument)[0]),
                            span(argument),
                        )
                    )
                elif argument.data == "calculation_keyword":
                    arguments.append(
                        Argument(
                            str(tokens(argument)[0]),
                            expression(branches(argument)[0]),
                            span(argument),
                        )
                    )
                else:
                    arguments.append(Argument("", expression(argument), span(argument)))
            result = Expression(
                "recover" if cases else "call",
                "",
                (selected,),
                tuple(arguments),
                tuple(cases),
                span(suffix),
            )
        return result
    # Precedence rules produce alternating expression and operator children.
    result = expression(parts[0])
    operators = [str(t) for t in node.children if isinstance(t, Token)]
    for operator, right in zip(operators, parts[1:], strict=True):
        result = Expression("binary", operator, (result, expression(right)), (), (), location)
    return result


def body(node: Tree[Token]) -> Body:
    """Lower a brace-delimited calculation with a mandatory final return.

    :param node: Computation rule.
    :returns: Immutable body without parser-specific objects.
    """
    bindings = tuple(
        Binding(str(tokens(item)[1]), expression(branches(item)[0]), span(item))
        for item in children(node, "let_statement")
    )
    returned = children(node, "return_statement")[0]
    return Body(bindings, expression(branches(returned)[0]), span(node))


def located_token(value: str, kind: str, location: Span) -> Token:
    """Restore a positioned semantic token without reading source text.

    :param value: Semantic token spelling.
    :param kind: Parser token category.
    :param location: Original calculation span.
    :returns: Positioned checker token.
    """
    return Token(
        kind,
        value,
        start_pos=location.start,
        end_pos=location.end,
        line=location.line,
        column=location.column,
        end_line=location.end_line,
        end_column=location.end_column,
    )


def restore_expression(node: Expression) -> Tree[Token]:
    """Adapt portable calculation nodes to the shared semantic checker.

    :param node: Structurally validated expression.
    :returns: Positioned checker syntax without parsing reconstructed source.
    """
    location = node.span
    if node.kind in ("name", "string", "integer", "decimal", "bool"):
        value = node.value
        if node.kind == "string":
            value = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        kind = {
            "name": "SELF" if value == "self" else "NAME",
            "string": "STRING",
            "integer": "CALC_NUMBER",
            "decimal": "CALC_NUMBER",
            "bool": "BOOLEAN",
        }[node.kind]
        return Tree(
            "calculation_name" if node.kind == "name" else "calculation_literal",
            [located_token(value, kind, location)],
        )
    if node.kind in ("member", "descriptor", "call", "recover"):
        selected = node.operands[0] if node.kind in ("call", "recover") else node
        operator = "::" if selected.kind == "descriptor" else "."
        suffix: list[Token | Tree[Token]] = [
            located_token(operator, "OP", selected.span),
            located_token(selected.value, "NAME", selected.span),
        ]
        if node.kind in ("call", "recover"):
            arguments: list[Token | Tree[Token]] = [located_token("(", "LPAR", location)]
            for argument in node.arguments:
                value_node = restore_expression(argument.expression)
                arguments.append(
                    Tree(
                        "calculation_keyword",
                        [
                            located_token(argument.name, "NAME", argument.span),
                            located_token("=", "EQUAL", argument.span),
                            value_node,
                        ],
                    )
                    if argument.name
                    else value_node
                )
            for case in node.cases:
                case_parts: list[Token | Tree[Token]] = [located_token("case", "CASE", case.span)]
                case_parts.extend(
                    located_token(status, "NAME", case.span) for status in case.statuses
                )
                if case.binding:
                    case_parts.extend(
                        [
                            located_token("as", "AS", case.span),
                            located_token(case.binding, "NAME", case.span),
                        ]
                    )
                case_parts.extend(
                    [
                        located_token("->", "ARROW", case.span),
                        restore_expression(case.expression),
                    ]
                )
                arguments.append(Tree("recovery_case", case_parts))
            arguments.append(located_token(")", "RPAR", location))
            suffix.append(Tree("calculation_arguments", arguments))
        return Tree(
            "calculation_postfix",
            [
                restore_expression(selected.operands[0]),
                Tree("calculation_suffix", suffix),
            ],
        )
    if node.kind == "lambda":
        return Tree(
            "lambda_expression",
            [
                located_token(node.value, "NAME", location),
                located_token("->", "ARROW", location),
                restore_expression(node.operands[0]),
            ],
        )
    if node.kind == "unary":
        return Tree(
            "unary_expression",
            [
                located_token(node.value, "OP", location),
                restore_expression(node.operands[0]),
            ],
        )
    if node.kind == "conditional":
        parts: list[Token | Tree[Token]] = []
        for keyword, operand in zip(("if", "then", "else"), node.operands, strict=True):
            parts.extend(
                [
                    located_token(keyword, keyword.upper(), location),
                    restore_expression(operand),
                ]
            )
        return Tree("conditional_expression", parts)
    return Tree(
        "sum_expression",
        [
            restore_expression(node.operands[0]),
            located_token(node.value, "OP", location),
            restore_expression(node.operands[1]),
        ],
    )


def restore_body(computation: Body) -> Tree[Token]:
    """Restore immutable locals and a final return for shared validation.

    :param computation: Portable calculation body.
    :returns: Checker computation node.
    """
    parts: list[Token | Tree[Token]] = [located_token("{", "LBRACE", computation.span)]
    for binding in computation.bindings:
        parts.append(
            Tree(
                "let_statement",
                [
                    located_token("let", "LET", binding.span),
                    located_token(binding.name, "NAME", binding.span),
                    located_token("=", "EQUAL", binding.span),
                    restore_expression(binding.expression),
                ],
            )
        )
    parts.append(
        Tree(
            "return_statement",
            [
                located_token("return", "RETURN", computation.result.span),
                restore_expression(computation.result),
            ],
        )
    )
    parts.append(located_token("}", "RBRACE", computation.span))
    return Tree("computation", parts)
