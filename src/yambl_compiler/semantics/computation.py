"""Type calculations and infer public inputs from their field dependencies."""

import re
from dataclasses import dataclass, field, replace
from typing import Literal

from lark import Token, Tree

from yambl_compiler.computation.records import Body, Expression, Span
from yambl_compiler.semantics.decorators import bind_call
from yambl_compiler.semantics.dimensions import (
    Dimension,
    field_dimension,
    resolve_dimension,
)
from yambl_compiler.semantics.entities import EntityIndex, FieldSite
from yambl_compiler.semantics.expressions import Resolver
from yambl_compiler.semantics.symbols import EntityKey, Unit, field_owner
from yambl_compiler.semantics.types import UNKNOWN, Resolved, accepts
from yambl_compiler.syntax.computation import body as calculation_body
from yambl_compiler.syntax.computation import span
from yambl_compiler.syntax.parser import Diagnostic, annotations, children, tokens

NUMBERS = ("integer", "decimal", "float")
IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")


@dataclass(frozen=True)
class Input:
    """Describe an inferred external argument.

    :param name: Public parameter name.
    :param type: Complete resolved constraint.
    :param required: Whether omission is invalid without a default.
    :param default: Literal or input-dependent default expression.
    :param sites: Source uses that contributed to this parameter.
    """

    name: str
    type: Resolved
    required: bool
    default: Expression | None
    sites: tuple[tuple[Unit, Span], ...] = ()


@dataclass(frozen=True)
class Access:
    """Link a calculation field access to its declaring implementation.

    :param span: Original field expression.
    :param owner: Declaring entity identity.
    :param name: Local field name.
    :param operation: at, best_available or computed value.
    """

    span: Span
    owner: EntityKey
    name: str
    operation: str


@dataclass(frozen=True)
class Calculation:
    """Expose a checked calculation and its inferred public contract.

    :param body: Independent calculation syntax.
    :param inputs: Canonical external parameter list.
    :param accesses: Resolved field uses.
    :param dependencies: Referenced computed implementations.
    """

    body: Body
    inputs: tuple[Input, ...]
    accesses: tuple[Access, ...]
    dependencies: tuple[tuple[EntityKey, str], ...]


@dataclass(frozen=True)
class Value:
    """Retain field identity until a descriptor is consumed as a value.

    :param type: Expression value type.
    :param site: Selected field, if not yet read.
    :param owner: Actual static receiver type.
    :param name: Selected local field name.
    """

    type: Resolved
    site: FieldSite | None = None
    owner: EntityKey | None = None
    name: str = ""


def structural(expression: Expression | None) -> object:
    """Compare expression meaning without source coordinates.

    :param expression: Optional default expression.
    :returns: Immutable semantic comparison key.
    """
    if expression is None:
        return None
    return (
        expression.kind,
        expression.value,
        tuple(structural(e) for e in expression.operands),
        tuple((a.name, structural(a.expression)) for a in expression.arguments),
        tuple((c.statuses, c.binding, structural(c.expression)) for c in expression.cases),
    )


@dataclass
class Computations:
    """Analyze reusable fields once while rejecting recursive dependencies.

    :param entities: Shared inheritance and field index.
    :param diagnostics: Shared diagnostic output.
    :param calculations: Completed implementation analyses.
    :param active: Implementations currently being analyzed.
    """

    entities: EntityIndex
    diagnostics: list[Diagnostic]
    calculations: dict[tuple[EntityKey, str], Calculation] = field(
        default_factory=dict[tuple[EntityKey, str], Calculation]
    )
    active: set[tuple[EntityKey, str]] = field(default_factory=set[tuple[EntityKey, str]])

    def analyze(self, owner: EntityKey, name: str, site: FieldSite) -> Calculation | None:
        """Analyze one implementation with memoization and cycle detection.

        :param owner: Declaration owner.
        :param name: Local field name.
        :param site: Original field syntax and scope.
        :returns: Checked calculation, or None for a recursive dependency.
        """
        key = (owner, name)
        if key in self.calculations:
            return self.calculations[key]
        field_body = children(site.declaration, "field_body")[0]
        syntax = children(field_body, "computation")[0]
        body = calculation_body(syntax)
        checker = Checker(self, owner, site.unit)
        if key in self.active:
            checker.issue(body.result, f"computed field dependency cycle: {name}")
            return None
        self.active.add(key)
        checker.declare_inputs(site.declaration)
        environment = {"self": Value(Resolved(owner[1], "reference", key=owner))}
        for binding in body.bindings:
            if (
                not IDENTIFIER.fullmatch(binding.name)
                or binding.name in environment
                or binding.name == "input"
            ):
                checker.issue(
                    binding.expression,
                    f"duplicate or invalid local binding: {binding.name}",
                )
            environment[binding.name] = checker.read(
                checker.infer(binding.expression, environment), binding.expression
            )
        actual = checker.read(checker.infer(body.result, environment), body.result).type
        expected = Resolver(site.unit, [], self.entities.field_types(owner)).resolve(
            children(field_body, "type_expr")[0]
        )
        if not checker.compatible(expected, actual):
            checker.issue(
                body.result,
                f"incompatible computed result: expected {expected.name}, got {actual.name}",
            )
        locals_: dict[str, Expression] = {}

        def expand(node: Expression) -> Expression:
            """Inline immutable locals in public default expressions.

            :param node: Default or earlier local expression.
            :returns: Expression independent of function-local variable names.
            """
            if node.kind == "name" and node.value in locals_:
                return locals_[node.value]
            return replace(
                node,
                operands=tuple(expand(n) for n in node.operands),
                arguments=tuple(
                    replace(a, expression=expand(a.expression)) for a in node.arguments
                ),
                cases=tuple(replace(c, expression=expand(c.expression)) for c in node.cases),
            )

        for binding in body.bindings:
            locals_[binding.name] = expand(binding.expression)
        checker.inputs = {
            name: replace(p, default=expand(p.default)) if p.default is not None else p
            for name, p in checker.inputs.items()
        }
        checker.check_defaults()
        result = Calculation(
            body,
            tuple(checker.inputs[n] for n in sorted(checker.inputs)),
            tuple(checker.accesses),
            tuple(sorted(checker.dependencies)),
        )
        self.active.remove(key)
        self.calculations[key] = result
        return result


class Checker:
    """Infer one calculation in its declaration's lexical and entity context."""

    def __init__(self, computations: Computations, owner: EntityKey, unit: Unit) -> None:
        """Create a local expression and input analysis context.

        :param computations: Shared dependency analysis.
        :param owner: Owning entity.
        :param unit: Declaring lexical scope.
        :returns: None.
        """
        self.computations = computations
        self.owner = owner
        self.unit = unit
        self.inputs: dict[str, Input] = {}
        self.declared: dict[str, Input] = {}
        self.accesses: list[Access] = []
        self.dependencies: set[tuple[EntityKey, str]] = set()

    def declare_inputs(self, declaration: Tree[Token]) -> None:
        """Read explicitly declared optional inputs before dependency inference.

        :param declaration: Computed field with checked metadata.
        :returns: None; records input contracts and declaration defaults.
        """
        for call in annotations(declaration, 'extra_input_params'):
            bound: list[tuple[str, str, Resolved]] = []
            bind_call(call, declaration, self.unit, [], bound_arguments=bound)
            pairs = children(call, 'pair_argument')
            if not pairs:
                continue
            location = span(pairs[0])
            for formal, name, type_ in bound:
                if formal != 'parameter':
                    continue
                node = Expression('name', name, (), (), (), location)
                if name in self.declared:
                    self.issue(node, f'duplicate extra input parameter: {name}')
                default_type = next(
                    (t for n, k, t in bound if n == 'parameter_default' and k == name), None
                )
                default = None
                if default_type is not None and default_type.exact is not None:
                    kind: Literal['string', 'integer', 'decimal', 'bool'] = (
                        'integer'
                        if type_.name in ('integer', 'snowflake_id')
                        else (
                            'decimal'
                            if type_.name in ('decimal', 'float')
                            else 'bool' if type_.name == 'bool' else 'string'
                        )
                    )
                    default = Expression(kind, default_type.exact, (), (), (), location)
                parameter = Input(name, type_, False, default, ((self.unit, location),))
                self.add_input(parameter, node)
                self.declared[name] = parameter

    def issue(self, node: Expression, message: str) -> None:
        """Append a located calculation error once.

        :param node: Offending expression.
        :param message: Diagnostic explanation.
        :returns: None.
        """
        diagnostic = Diagnostic(self.unit.document.path, node.span.line, node.span.column, message)
        if diagnostic not in self.computations.diagnostics:
            self.computations.diagnostics.append(diagnostic)

    def compatible(self, expected: Resolved, actual: Resolved) -> bool:
        """Check scalar promotion and declared collection contracts.

        :param expected: Required contract.
        :param actual: Inferred value.
        :returns: Whether the value is compatible.
        """
        if expected.name in NUMBERS and actual.name in NUMBERS:
            return NUMBERS.index(actual.name) <= NUMBERS.index(expected.name)
        if actual.kind == "unknown" and actual.name == "non_normal":
            return True
        if (
            expected.name == actual.name
            and expected.args
            and len(expected.args) == len(actual.args)
        ):
            return all(
                self.compatible(a, b) for a, b in zip(expected.args, actual.args, strict=True)
            )
        return accepts(expected, actual, self.unit.world)

    def add_input(self, requirement: Input, node: Expression) -> None:
        """Merge a public input by name, retaining every contributing use.

        :param requirement: Dependency requirement.
        :param node: Binding or use expression.
        :returns: None.
        """
        name = requirement.name
        if not IDENTIFIER.fullmatch(name) or name in ("self", "input", "best_effort"):
            self.issue(node, f"invalid or reserved calculation parameter: {name}")
        prior = self.inputs.get(name)
        declared = self.declared.get(name)
        if declared is not None:
            requirement = replace(
                requirement, required=False, default=declared.default or requirement.default
            )
        if prior is not None:
            if prior.type != requirement.type or (
                prior.default is not None
                and requirement.default is not None
                and structural(prior.default) != structural(requirement.default)
            ):
                self.issue(node, f"conflicting inferred parameter: {name}")
            requirement = replace(
                prior,
                required=prior.required or requirement.required,
                default=(prior.default if prior.default is not None else requirement.default),
                sites=prior.sites + requirement.sites,
            )
        self.inputs[name] = requirement

    def bind_input(
        self, requirement: Input, expression: Expression, environment: dict[str, Value]
    ) -> None:
        """Bind a dependency to a public input or an internal expression.

        :param requirement: Required dependency parameter.
        :param expression: Explicit binding expression.
        :param environment: Local calculation values.
        :returns: None.
        """
        alias = expression
        default = None
        if expression.kind == "binary" and expression.value == "??":
            alias, default = expression.operands
        if (
            alias.kind == "member"
            and alias.operands[0].kind == "name"
            and alias.operands[0].value == "input"
        ):
            self.add_input(
                Input(
                    alias.value,
                    requirement.type,
                    requirement.required,
                    default if default is not None else requirement.default,
                    ((self.unit, expression.span),),
                ),
                expression,
            )
            if default is not None:
                saved = dict(self.inputs)
                self.bind_input(
                    replace(requirement, default=None, required=True),
                    default,
                    environment,
                )
                if alias.value in self.declared and self.declared[alias.value].default is not None:
                    self.inputs = saved
            return
        actual = self.read(self.infer(expression, environment), expression).type
        # A contextual reference ID is a value, not a lookup during compilation.
        if requirement.type.kind == "reference" and actual.name == "string":
            return
        if not self.compatible(requirement.type, actual):
            self.issue(expression, f"incompatible binding for parameter: {requirement.name}")

    def check_defaults(self) -> None:
        """Reject cycles in inferred input defaults.

        :returns: None.
        """

        def references(node: Expression) -> set[str]:
            """Collect external names in a default.

            :param node: Default expression.
            :returns: Referenced public inputs.
            """
            if (
                node.kind == "member"
                and node.operands[0].kind == "name"
                and node.operands[0].value == "input"
            ):
                return {node.value}
            return set[str]().union(
                *(references(n) for n in node.operands),
                *(references(a.expression) for a in node.arguments),
            )

        edges: dict[str, set[str]] = {
            name: references(p.default) if p.default else set() for name, p in self.inputs.items()
        }
        for name, parameter in self.inputs.items():
            pending = list(edges[name])
            seen: set[str] = set()
            while pending:
                dependency = pending.pop()
                if dependency == name:
                    assert parameter.default is not None
                    self.issue(
                        parameter.default,
                        f"calculation parameter default cycle: {name}",
                    )
                    break
                if dependency not in seen:
                    seen.add(dependency)
                    pending.extend(edges.get(dependency, ()))

    def field_parameters(self, value: Value, operation: str, node: Expression) -> list[Input]:
        """Read the input contract of a selected field operation.

        :param value: Selected field descriptor.
        :param operation: Requested field operation.
        :param node: Source invocation.
        :returns: Required and optional dependencies.
        """
        assert value.site is not None and value.owner is not None
        site = value.site
        body = children(site.declaration, "field_body")[0]
        mode = str(tokens(body)[0])
        declared = field_owner(site.unit, children(body, "type_expr")[0])
        owner = declared.key if declared else value.owner
        self.accesses.append(Access(node.span, owner, value.name, operation))
        if mode == "computed":
            if operation != "value":
                self.issue(node, "computed fields support ::value, not stored-field operations")
            self.dependencies.add((owner, value.name))
            calculation = self.computations.analyze(owner, value.name, site)
            return list(calculation.inputs) if calculation else []
        if operation not in ("at", "best_available"):
            self.issue(node, "stored fields support ::at or ::best_available")
        result: list[Input] = []
        if mode in ("snapshot", "scd") and not annotations(site.declaration, "temporal"):
            result.append(
                Input(
                    "as_of_date",
                    Resolved("date"),
                    True,
                    None,
                    ((self.unit, node.span),),
                )
            )
        for call in annotations(site.declaration, "temporal"):
            for pair in children(call, "pair_argument"):
                result.append(
                    Input(
                        str(tokens(pair)[0]),
                        Resolved("date"),
                        True,
                        None,
                        ((self.unit, node.span),),
                    )
                )
        for call in annotations(site.declaration, "dimension"):
            dimension = field_dimension(site.unit, site.declaration, call, [])
            if dimension is None:
                continue
            best = operation == "best_available" and bool(dimension.hierarchy_name)
            result.append(
                Input(
                    dimension.value_name,
                    dimension.value_type,
                    not best,
                    None,
                    ((self.unit, node.span),),
                )
            )
            if best:
                result.append(
                    Input(
                        dimension.hierarchy_name,
                        Resolved(
                            "Hierarchy",
                            "reference",
                            key=site.unit.scope["Hierarchy"].key,
                        ),
                        False,
                        dimension.hierarchy_default,
                        ((self.unit, node.span),),
                    )
                )
        return result

    def read(self, value: Value, node: Expression) -> Value:
        """Consume a plain field in value position and infer its unbound inputs.

        :param value: Expression or unconsumed field descriptor.
        :param node: Use site.
        :returns: Value with dependencies collected.
        """
        if value.site is not None:
            mode = str(tokens(children(value.site.declaration, "field_body")[0])[0])
            for parameter in self.field_parameters(
                value, "value" if mode == "computed" else "at", node
            ):
                self.add_input(parameter, node)
        return Value(value.type)

    def infer(self, node: Expression, environment: dict[str, Value]) -> Value:
        """Infer an expression while preserving lazy branch structure.

        :param node: Independent calculation AST.
        :param environment: Immutable local names and lambda parameters.
        :returns: Inferred value or unresolved field descriptor.
        """
        if node.kind in ("string", "integer", "decimal", "bool"):
            return Value(
                Resolved(
                    node.kind,
                    "string" if node.kind == "string" else "value",
                    exact=node.value,
                )
            )
        if node.kind == "name":
            if node.value in environment:
                return environment[node.value]
            if node.value in self.unit.world.constants and node.value != "PARTIAL":
                return Value(Resolved("non_normal", "unknown"))
            self.issue(node, f"unknown calculation name: {node.value}")
            return Value(UNKNOWN)
        if node.kind == "member":
            receiver = node.operands[0]
            if receiver.kind == 'name' and receiver.value == 'input' and node.value in self.inputs:
                return Value(self.inputs[node.value].type)
            if (
                receiver.kind == "name"
                and receiver.value in self.unit.scope
                and self.unit.scope[receiver.value].kind == "decodes"
            ):
                name = Token(
                    "NAME",
                    receiver.value,
                    line=receiver.span.line,
                    column=receiver.span.column,
                )
                code = Token("NAME", node.value, line=node.span.line, column=node.span.column)
                syntax: Tree[Token] = Tree(
                    "type_expr", [name, Tree("access", [Token("DOT", "."), code])]
                )
                return Value(
                    Resolver(self.unit, self.computations.diagnostics, {}).evaluate(syntax).type
                )
            parent = self.read(self.infer(receiver, environment), receiver)
            if parent.type.name == "partial" and node.value in ("value", "issues"):
                return Value(
                    parent.type.args[0]
                    if node.value == "value"
                    else Resolved("issues", "collection")
                )
            if parent.type.kind != "reference" or parent.type.key is None:
                self.issue(node, "member access requires a scalar entity reference")
                return Value(UNKNOWN)
            site = self.computations.entities.effective.get(parent.type.key, {}).get(node.value)
            if site is None:
                index = self.computations.entities
                root = (index.ancestors.get(parent.type.key, []) or [parent.type.key])[-1]
                site = next(
                    (
                        fields[node.value]
                        for key, fields in index.effective.items()
                        if (key == root or root in index.ancestors.get(key, []))
                        and node.value in fields
                    ),
                    None,
                )
            if site is None:
                self.issue(node, f"unknown entity field: {node.value}")
                return Value(UNKNOWN)
            syntax = children(children(site.declaration, "field_body")[0], "type_expr")[0]
            resolved = Resolver(
                site.unit,
                self.computations.diagnostics,
                self.computations.entities.field_types(parent.type.key),
                True,
            ).resolve(syntax)
            return Value(resolved, site, parent.type.key, node.value)
        if node.kind in ("call", "recover"):
            return self.invoke(node, environment)
        if node.kind in ("binary", "unary"):
            if node.value == '??':
                input_node = node.operands[0]
                if (
                    input_node.kind == 'member'
                    and input_node.operands[0].kind == 'name'
                    and input_node.operands[0].value == 'input'
                    and input_node.value in self.inputs
                ):
                    parameter = self.inputs[input_node.value]
                    self.bind_input(parameter, node, environment)
                    return Value(parameter.type)
                self.issue(node, '?? requires an input.name with an inferred or declared type')
                return Value(UNKNOWN)
            operands = [self.read(self.infer(e, environment), e).type for e in node.operands]
            if node.value in ("and", "or", "not"):
                if any(t.name != "bool" and t.kind != "unknown" for t in operands):
                    self.issue(node, "boolean operators require bool operands")
                return Value(Resolved("bool"))
            if node.value in ("==", "!=", "<", "<=", ">", ">="):
                if len(operands) == 2 and not (
                    self.compatible(operands[0].base(), operands[1])
                    or self.compatible(operands[1].base(), operands[0])
                ):
                    self.issue(node, "comparison requires compatible operand types")
                return Value(Resolved("bool"))
            if any(t.name not in NUMBERS and t.kind != "unknown" for t in operands):
                self.issue(node, "arithmetic requires integer, decimal or float operands")
                return Value(UNKNOWN)
            return Value(
                Resolved(
                    max(
                        (t.name for t in operands if t.name in NUMBERS),
                        key=NUMBERS.index,
                        default="integer",
                    )
                )
            )
        if node.kind == "conditional":
            condition = self.read(self.infer(node.operands[0], environment), node.operands[0])
            if condition.type.name != "bool" and condition.type.kind != "unknown":
                self.issue(node, "if condition requires bool")
            left, right = [self.read(self.infer(e, environment), e).type for e in node.operands[1:]]
            if self.compatible(left.base(), right):
                return Value(left.base())
            if self.compatible(right.base(), left):
                return Value(right.base())
            self.issue(node, "if branches require compatible result types")
            return Value(UNKNOWN)
        self.issue(node, "expression is only valid in its binding or method context")
        return Value(UNKNOWN)

    def invoke(self, node: Expression, environment: dict[str, Value]) -> Value:
        """Check field operations, collection methods and recovery branches.

        :param node: Call or recovery node.
        :param environment: Local values.
        :returns: Method result type.
        """
        selected = node.operands[0]
        if selected.kind not in ("member", "descriptor") or not selected.operands:
            self.issue(node, "expression is only valid in its binding or method context")
            return Value(UNKNOWN)
        receiver = selected.operands[0]
        method = selected.value
        if selected.kind == "descriptor":
            value = self.infer(receiver, environment)
            if value.site is None:
                self.issue(node, "field operation requires a field descriptor")
                return Value(UNKNOWN)
            parameters = self.field_parameters(value, method, node)
            explicit = {a.name: a.expression for a in node.arguments}

            def substituted(expression: Expression) -> Expression:
                """Rebind child default dependencies within this invocation subtree.

                :param expression: Child default expression.
                :returns: Default expressed in the caller's binding context.
                """
                if (
                    expression.kind == "member"
                    and expression.operands[0].kind == "name"
                    and expression.operands[0].value == "input"
                ):
                    return explicit.get(expression.value, expression)
                return replace(
                    expression,
                    operands=tuple(substituted(e) for e in expression.operands),
                    arguments=tuple(
                        replace(a, expression=substituted(a.expression))
                        for a in expression.arguments
                    ),
                )

            parameters = [
                replace(p, default=substituted(p.default)) if p.default else p for p in parameters
            ]
            requirements = {p.name: p for p in parameters}
            supplied: set[str] = set()
            for argument in node.arguments:
                if argument.name not in requirements or argument.name in supplied:
                    self.issue(
                        argument.expression,
                        f"unknown or duplicate field argument: {argument.name}",
                    )
                    continue
                supplied.add(argument.name)
                self.bind_input(requirements[argument.name], argument.expression, environment)
            for parameter in parameters:
                if parameter.name not in supplied:
                    self.add_input(parameter, node)
            if method == "best_available":
                automatic: list[Dimension] = []
                for call in annotations(value.site.declaration, "dimension"):
                    dimension = field_dimension(value.site.unit, value.site.declaration, call, [])
                    if (
                        dimension
                        and dimension.hierarchy_name
                        and dimension.value_name not in supplied
                    ):
                        automatic.append(dimension)
                if len(automatic) > 1:
                    self.issue(
                        node,
                        "best_available supports one automatic Hierarchy; bind the other dimension values",
                    )
            return Value(value.type)
        value = self.read(self.infer(receiver, environment), receiver)
        if method == "recover":
            if node.arguments or not node.cases:
                self.issue(node, "recover requires case branches")
            seen: set[str] = set()
            for case in node.cases:
                for status in case.statuses:
                    if status not in self.unit.world.constants and status != "PARTIAL":
                        self.issue(case.expression, f"unknown recovery status: {status}")
                    if status in seen:
                        self.issue(case.expression, f"duplicate recovery status: {status}")
                    seen.add(status)
                local = dict(environment)
                if case.binding:
                    if case.statuses != ("PARTIAL",) or case.binding in local:
                        self.issue(
                            case.expression,
                            "recovery binding requires a unique PARTIAL case variable",
                        )
                    local[case.binding] = Value(Resolved("partial", args=(value.type,)))
                alternative = self.read(self.infer(case.expression, local), case.expression)
                if not self.compatible(value.type.base(), alternative.type):
                    self.issue(case.expression, "incompatible recovery branch result")
            return Value(value.type.base())
        if method not in ("map", "filter", "flat_map", "group_by", "sum_by"):
            self.issue(node, f"unknown calculation method: {method}")
            return Value(UNKNOWN)
        if (
            len(node.arguments) != 1
            or node.arguments[0].name
            or node.arguments[0].expression.kind != "lambda"
        ):
            self.issue(node, f"{method} requires one lambda argument")
            return Value(UNKNOWN)
        if (
            value.type.name not in ("collection", "values", "sequence", "grouped")
            or not value.type.args
        ):
            self.issue(node, f"{method} requires a sequence")
            return Value(UNKNOWN)
        grouped = value.type.name == "grouped"
        if grouped and method != "sum_by":
            self.issue(node, "grouped values support sum_by")
        function = node.arguments[0].expression
        if function.value in environment or not IDENTIFIER.fullmatch(function.value):
            self.issue(function, "lambda requires a unique snake_case parameter")
        local = {**environment, function.value: Value(value.type.args[-1])}
        returned = self.read(
            self.infer(function.operands[0], local), function.operands[0]
        ).type.base()
        if method == "filter":
            if returned.name != "bool" and returned.kind != "unknown":
                self.issue(function, "filter predicate requires bool")
            return value
        if method == "flat_map":
            if returned.name not in ("sequence", "collection", "values") or not returned.args:
                self.issue(function, "flat_map requires a sequence result")
                return Value(UNKNOWN)
            return Value(Resolved("sequence", "collection", args=returned.args))
        if method == "group_by":
            if returned.kind == "collection":
                self.issue(function, "group_by requires a scalar key")
            return Value(Resolved("grouped", "collection", args=(returned, value.type.args[0])))
        if method == "sum_by":
            if returned.name not in NUMBERS and returned.kind != "unknown":
                self.issue(function, "sum_by requires numeric contributions")
            return Value(
                Resolved("mapping", "collection", args=(value.type.args[0], returned))
                if grouped
                else returned
            )
        return Value(Resolved("sequence", "collection", args=(returned,)))


def check_computations(
    entities: EntityIndex, diagnostics: list[Diagnostic], units: list[Unit]
) -> dict[tuple[EntityKey, str], Calculation]:
    """Analyze every computed implementation and compare inherited contracts.

    :param entities: Shared field index.
    :param diagnostics: Diagnostic destination.
    :param units: Resolved scopes containing dimension policy definitions.
    :returns: Computed implementations with inferred signatures.
    """
    analyzer = Computations(entities, diagnostics)
    visited: set[tuple[EntityKey, str]] = set()
    for unit in units:
        for owner, dimensions in unit.world.dimensions.items():
            for name in dimensions:
                if (owner, name) in visited:
                    continue
                visited.add((owner, name))
                dimension = resolve_dimension(unit, owner, name, [])
                if dimension is None or dimension.fallback is None:
                    continue
                checker = Checker(analyzer, owner, dimension.unit)
                actual = checker.read(
                    checker.infer(
                        dimension.fallback,
                        {"self": Value(Resolved(owner[1], "reference", key=owner))},
                    ),
                    dimension.fallback,
                )
                if actual.type.name != "integer" and actual.type.kind != "unknown":
                    checker.issue(
                        dimension.fallback,
                        "fallback_days requires an integer expression",
                    )
                if checker.inputs or checker.dependencies:
                    checker.issue(
                        dimension.fallback,
                        "fallback_days requires stored static or config dependencies without external parameters",
                    )
    for owner, fields in entities.fields.items():
        for name, site in fields.items():
            if children(children(site.declaration, "field_body")[0], "computation"):
                analyzer.analyze(owner, name, site)
    contracts: dict[tuple[EntityKey, str], tuple[object, ...]] = {}
    for key, calculation in analyzer.calculations.items():
        logical = entities.identities.get(key, key)
        contract = tuple(
            (p.name, p.type, p.required, structural(p.default)) for p in calculation.inputs
        )
        if logical in contracts and contracts[logical] != contract:
            site = entities.fields[key[0]][key[1]]
            Checker(analyzer, key[0], site.unit).issue(
                calculation.body.result,
                f"computed parameter signature mismatch: {key[1]}",
            )
        contracts[logical] = contract
    return analyzer.calculations
