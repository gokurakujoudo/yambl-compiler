"""Translate positioned calculation syntax into immutable expression records."""

from typing import Literal, cast

from lark import Token, Tree

from yambl_compiler.languages.yambl.computation.records import (
    Argument,
    Binding,
    Body,
    Case,
    Expression,
    HintAccess,
    HintOperation,
    HintParameter,
    Span,
    TypeHint,
)
from yambl_compiler.languages.yambl.syntax.parser import children, string_value, tokens


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


def type_hint(node: Tree[Token]) -> TypeHint:
    """Lower a local type annotation into generic and primitive constraints.

    :param node: Annotation syntax.
    :returns: Independent type structure.
    """
    parameters: list[HintParameter] = []
    for group in children(node, 'parameters'):
        for item in group.children:
            if isinstance(item, Tree):
                ts = tokens(item)
                parameters.append(HintParameter(str(ts[0]), str(ts[-1])))
            elif item.type in ('INT', 'STRING'):
                parameters.append(HintParameter('', str(item)))
    return TypeHint(
        str(tokens(node)[0]),
        tuple(type_hint(a) for g in children(node, 'arguments') for a in children(g, 'type_expr')),
        tuple(parameters),
        tuple(
            HintAccess(cast(Literal['.', '::'], str(tokens(a)[0])), str(tokens(a)[1]))
            for a in children(node, 'access')
        ),
        span(node),
        operations=tuple(
            HintOperation(
                cast(Literal['+', '-'], str(tokens(o)[0])),
                type_hint(children(o, 'flag_operand')[0]),
            )
            for o in children(node, 'flag_operation')
        ),
        binding=str(tokens(children(node, 'binding')[0])[-1]) if children(node, 'binding') else '',
    )


def restore_hint(hint: TypeHint) -> Tree[Token]:
    """Restore a checked type annotation without parsing source.

    :param hint: Portable type syntax.
    :returns: Semantic type expression.
    """

    def literal(value: str) -> Token:
        """Restore a type atom.

        :param value: Atom spelling.
        :returns: Positioned token.
        """
        kind = (
            'STRING' if value.startswith("'") else 'INT' if value.lstrip('-').isdigit() else 'NAME'
        )
        return located_token(value, kind, hint.span)

    parts: list[Token | Tree[Token]] = [literal(hint.name)]
    if hint.arguments:
        parts.append(Tree('arguments', [restore_hint(a) for a in hint.arguments]))
    if hint.parameters:
        parts.append(
            Tree(
                'parameters',
                [
                    (
                        Tree(
                            'named_parameter',
                            [
                                literal(p.name),
                                located_token(':', 'COLON', hint.span),
                                literal(p.value),
                            ],
                        )
                        if p.name
                        else literal(p.value)
                    )
                    for p in hint.parameters
                ],
            )
        )
    parts.extend(
        Tree('access', [located_token(a.operator, 'OP', hint.span), literal(a.name)])
        for a in hint.accesses
    )
    parts.extend(
        Tree(
            'flag_operation',
            [
                located_token(o.operator, 'OP', hint.span),
                Tree('flag_operand', restore_hint(o.operand).children),
            ],
        )
        for o in hint.operations
    )
    if hint.binding:
        parts.append(Tree('binding', [located_token('as', 'AS', hint.span), literal(hint.binding)]))
    return Tree('type_expr', parts)


def expression(node: Tree[Token]) -> Expression:
    """Lower a calculation without performing name or type resolution.

    :param node: Calculation syntax root.
    :returns: Explicit typed syntax with lazy branches retained.
    """
    location = span(node)
    parts = branches(node)
    items = tokens(node)
    kind = str(node.data)
    if kind == 'calculation_sequence':
        return Expression('sequence', '', tuple(expression(p) for p in parts), (), (), location)
    if kind == 'calculation_object':
        return Expression(
            'object',
            '',
            tuple(expression(p) for pair in parts for p in branches(pair)),
            (),
            (),
            location,
        )
    if kind == "calculation_name":
        return Expression("name", str(items[0]), (), (), (), location)
    if kind == 'calculation_function':
        function_arguments = tuple(
            Argument(
                str(tokens(argument)[0]) if argument.data == 'calculation_keyword' else '',
                expression(
                    branches(argument)[0] if argument.data == 'calculation_keyword' else argument
                ),
                span(argument),
            )
            for argument in branches(parts[0])
        )
        return Expression(
            'call',
            '',
            (Expression('name', str(items[0]), (), (), (), location),),
            function_arguments,
            (),
            location,
        )
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
        Binding(
            str(tokens(item)[1]),
            expression(branches(item)[-1]),
            span(item),
            type_hint(children(item, 'type_expr')[0]) if children(item, 'type_expr') else None,
        )
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
    if node.kind == 'sequence':
        return Tree(
            'calculation_sequence',
            [
                located_token('[', 'LSQB', location),
                *(restore_expression(o) for o in node.operands),
                located_token(']', 'RSQB', location),
            ],
        )
    if node.kind == 'object':
        return Tree(
            'calculation_object',
            [
                located_token('{', 'LBRACE', location),
                *(
                    Tree[Token](
                        'calculation_pair',
                        [
                            restore_expression(node.operands[i]),
                            located_token(':', 'COLON', location),
                            restore_expression(node.operands[i + 1]),
                        ],
                    )
                    for i in range(0, len(node.operands), 2)
                ),
                located_token('}', 'RBRACE', location),
            ],
        )
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
            if selected.kind == 'name':
                return Tree(
                    'calculation_function',
                    [
                        located_token(selected.value, 'NAME', selected.span),
                        Tree('function_arguments', arguments),
                    ],
                )
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
                    *([restore_hint(binding.annotation)] if binding.annotation else []),
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
