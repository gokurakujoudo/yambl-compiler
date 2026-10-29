"""Check named lookup paths and infer their preparation parameters."""

from dataclasses import dataclass, replace

from lark import Token, Tree

from yambl_compiler.languages.yambl.computation.evaluation import evaluate
from yambl_compiler.languages.yambl.computation.records import Expression
from yambl_compiler.languages.yambl.semantics.computation import (
    IDENTIFIER,
    Access,
    Checker,
    Computations,
    Input,
    Value,
)
from yambl_compiler.languages.yambl.semantics.entities import EntityIndex
from yambl_compiler.languages.yambl.semantics.expressions import Resolver
from yambl_compiler.languages.yambl.semantics.symbols import EntityKey, Unit, declaration_name
from yambl_compiler.languages.yambl.semantics.types import UNKNOWN, Resolved, accepts, common_type
from yambl_compiler.languages.yambl.syntax.computation import branches, expression
from yambl_compiler.languages.yambl.syntax.parser import (
    Diagnostic,
    annotations,
    children,
    diagnostic,
    tokens,
)


@dataclass(frozen=True)
class LookupParameter:
    """Describe one field parameter and its explicit binding.

    :param requirement: Original field parameter contract.
    :param binding: Explicit binding, or None for an inferred same-name input.
    :param input_name: Preparation input name, or empty for a fixed value.
    :param value: Evaluated fixed value, or None for an input.
    """

    requirement: Input
    binding: Expression | None
    input_name: str
    value: object | None


@dataclass(frozen=True)
class LookupStep:
    """Describe a selected field and its parameter bindings.

    :param access: Field declaration and source position.
    :param parameters: Field parameters in declaration order.
    """

    access: Access
    parameters: tuple[LookupParameter, ...]


@dataclass(frozen=True)
class LookupKey:
    """Describe one external key and its checked field path.

    :param name: External input field name.
    :param type: Resolved field value type.
    :param expression: Original path and parameter bindings.
    :param accesses: Fields selected by the path in evaluation order.
    :param steps: Field parameters and bindings in evaluation order.
    """

    name: str
    type: Resolved
    expression: Expression
    accesses: tuple[Access, ...]
    steps: tuple[LookupStep, ...]


@dataclass(frozen=True)
class Lookup:
    """Describe a rule in its declaring entity and lexical scope.

    :param owner: Entity declaring the rule, including extension targets.
    :param name: Rule name.
    :param unit: Declaration scope.
    :param node: Declaration syntax and location.
    :param declaration: Enclosing entity or extension declaration.
    :param keys: External keys in declaration order.
    :param inputs: Required preparation parameters in name order.
    """

    owner: EntityKey
    name: str
    unit: Unit
    node: Tree[Token]
    declaration: Tree[Token]
    keys: tuple[LookupKey, ...]
    inputs: tuple[Input, ...]


class LookupChecker(Checker):
    """Restrict calculation inference to lookup paths and fixed bindings."""

    def __init__(self, computations: Computations, owner: EntityKey, unit: Unit) -> None:
        """Initialize field step collection.

        :param computations: Shared field and constant analysis.
        :param owner: Entity declaring the rule.
        :param unit: Declaration scope.
        :returns: None.
        """
        super().__init__(computations, owner, unit)
        self.steps: list[LookupStep] = []
        self.descriptor_error = False

    def issue(self, node: Expression, message: str) -> None:
        """Avoid repeating a descriptor error caused by the same invalid path.

        :param node: Offending expression.
        :param message: Diagnostic text.
        :returns: None; appends a diagnostic when needed.
        """
        if message == 'field operation requires a field descriptor':
            if self.descriptor_error:
                return
            self.descriptor_error = True
        super().issue(node, message)

    def add_input(self, requirement: Input, node: Expression) -> None:
        """Merge compatible preparation parameters under their public name.

        :param requirement: Required parameter and its source.
        :param node: Current use.
        :returns: None; updates the inferred parameters.
        """
        prior = self.inputs.get(requirement.name)
        if prior is not None and prior.type != requirement.type:
            if accepts(prior.type, requirement.type, self.unit.world):
                self.inputs[requirement.name] = replace(prior, type=requirement.type)
            elif accepts(requirement.type, prior.type, self.unit.world):
                requirement = replace(requirement, type=prior.type)
        super().add_input(requirement, node)

    def infer(
        self, node: Expression, environment: dict[str, Value], expected: Resolved | None = None
    ) -> Value:
        """Restrict field selection to the receiver's possible actual types.

        :param node: Expression being checked.
        :param environment: Lookup self binding.
        :param expected: Contextual type when checking a fixed binding.
        :returns: Resolved value or unknown after a diagnostic.
        """
        result = super().infer(node, environment, expected)
        if node.kind != 'member' or result.site is None or result.owner is None:
            return result
        index = self.computations.entities
        candidates = [result.owner] + [
            key for key, chain in index.ancestors.items() if result.owner in chain
        ]
        site = next(
            (
                index.effective[key][result.name]
                for key in candidates
                if result.name in index.effective.get(key, {})
            ),
            None,
        )
        if site is None:
            self.issue(node, f'unknown entity field: {result.name}')
            return Value(UNKNOWN)
        syntax = children(children(site.declaration, 'field_body')[0], 'type_expr')[0]
        resolved = Resolver(
            site.unit, self.computations.diagnostics, index.field_types(result.owner), True
        ).resolve(syntax)
        types: list[Resolved] = []
        for candidate in candidates:
            candidate_site = index.effective.get(candidate, {}).get(result.name)
            if candidate_site is not None:
                candidate_syntax = children(
                    children(candidate_site.declaration, 'field_body')[0], 'type_expr'
                )[0]
                types.append(
                    Resolver(candidate_site.unit, [], index.field_types(candidate), True).resolve(
                        candidate_syntax
                    )
                )
        resolved = common_type(types, self.unit.world) or resolved
        return Value(resolved, site, result.owner, result.name)

    def field_parameters(self, value: Value, operation: str, node: Expression) -> list[Input]:
        """Collect a stored field's required date and dimensions.

        :param value: Selected field descriptor.
        :param operation: Requested operation.
        :param node: Source expression.
        :returns: Required parameters.
        """
        assert value.site is not None
        body = children(value.site.declaration, 'field_body')[0]
        mode = str(tokens(body)[0])
        if mode not in ('id', 'attr', 'static', 'scd'):
            self.issue(node, f'lookup does not support {mode} fields')
        parameters = super().field_parameters(value, operation, node)
        bindings = {argument.name: argument.expression for argument in node.arguments}
        self.steps.append(
            LookupStep(
                self.accesses[-1],
                tuple(
                    LookupParameter(parameter, bindings.get(parameter.name), '', None)
                    for parameter in parameters
                ),
            )
        )
        return parameters

    def bind_input(
        self, requirement: Input, expression: Expression, environment: dict[str, Value]
    ) -> None:
        """Bind a parameter to a public input or a compile-time value.

        :param requirement: Field parameter contract.
        :param expression: Binding expression.
        :param environment: Lookup self binding.
        :returns: None; records an input or checks a fixed value.
        """
        if expression.kind == 'member' and expression.operands[0].kind == 'name':
            root = expression.operands[0].value
            if root == 'input':
                super().bind_input(requirement, expression, environment)
                return
            symbol = self.unit.scope.get(root)
            fixed = symbol is not None and symbol.kind == 'decodes'
        elif expression.kind == 'name':
            symbol = self.unit.scope.get(expression.value)
            fixed = symbol is not None and symbol.kind == 'constant'
        else:
            fixed = expression.kind in ('string', 'integer', 'decimal', 'bool')
        if not fixed:
            self.issue(
                expression,
                'lookup binding requires a literal, constant, dictionary member or input',
            )
            return
        actual = self.read(self.infer(expression, environment, requirement.type), expression).type
        if expression.kind == 'string' and requirement.type.name == 'string':
            actual = replace(requirement.type, exact=expression.value)
        if not self.compatible(requirement.type, actual):
            self.issue(expression, f'incompatible binding for parameter: {requirement.name}')

    def finish_step(self, step: LookupStep) -> LookupStep:
        """Resolve fixed values and input names after checking a field path.

        :param step: Field and its checked bindings.
        :returns: Step with preparation sources and evaluated fixed values.
        """

        def resolve(name: str) -> object:
            """Find a constant or dictionary member in the rule's scope.

            :param name: Constant name or dictionary member path.
            :returns: Compile-time value.
            :raises ValueError: If the dependency was not evaluated.
            """
            root, dot, member = name.partition('.')
            symbol = self.unit.scope.get(root)
            if symbol is not None and symbol.kind == 'constant' and not dot:
                self.computations.constant(symbol.key, self.unit)
                if symbol.key in self.computations.constant_values:
                    return self.computations.constant_values[symbol.key]
            if symbol is not None and symbol.kind == 'decodes' and dot:
                return member
            raise ValueError(f'cannot evaluate lookup binding: {name}')

        result: list[LookupParameter] = []
        for parameter in step.parameters:
            binding = parameter.binding
            if binding is None:
                result.append(replace(parameter, input_name=parameter.requirement.name))
            elif (
                binding.kind == 'member'
                and binding.operands[0].kind == 'name'
                and binding.operands[0].value == 'input'
            ):
                result.append(replace(parameter, input_name=binding.value))
            elif (
                binding.kind in ('string', 'integer', 'decimal', 'bool')
                or (
                    binding.kind == 'name'
                    and (symbol := self.unit.scope.get(binding.value)) is not None
                    and symbol.kind == 'constant'
                )
                or (
                    binding.kind == 'member'
                    and binding.operands[0].kind == 'name'
                    and (symbol := self.unit.scope.get(binding.operands[0].value)) is not None
                    and symbol.kind == 'decodes'
                )
            ):
                try:
                    result.append(
                        replace(parameter, value=evaluate(self.elaborate(binding), resolve))
                    )
                except ValueError as error:
                    self.issue(binding, str(error))
                    result.append(parameter)
            else:
                result.append(parameter)
        return replace(step, parameters=tuple(result))


def path(node: Expression) -> bool:
    """Recognize a self-rooted field path with optional value calls.

    :param node: Calculation expression.
    :returns: Whether the expression has the supported path shape.
    """
    if node.kind == 'name':
        return node.value == 'self'
    if node.kind == 'member':
        return path(node.operands[0])
    if node.kind == 'call':
        selected = node.operands[0]
        return (
            selected.kind == 'descriptor'
            and selected.value == 'value'
            and selected.operands[0].kind == 'member'
            and all(argument.name for argument in node.arguments)
            and path(selected.operands[0])
        )
    return False


def supported(type_: Resolved, unit: Unit) -> bool:
    """Check whether the current storage backends can read a scalar type.

    :param type_: Resolved field type.
    :param unit: Lookup's compilation scope.
    :returns: Whether a supported scalar or entity reference is declared.
    """
    if type_.kind == 'unknown':
        return True
    if type_.dependency:
        return False
    if type_.kind == 'reference':
        return True
    if type_.name == 'code':
        key = type_.args[0].key if type_.args else None
        declaration = unit.world.declarations.get(key) if key else None
        return declaration is None or not annotations(declaration[1], 'flags')
    return type_.key is None and type_.name in (
        'string',
        'bool',
        'integer',
        'snowflake_id',
        'decimal',
        'date',
    )


def name_diagnostic(unit: Unit, node: Tree[Token], message: str) -> Diagnostic:
    """Locate a rule diagnostic at its name.

    :param unit: Declaration scope.
    :param node: Lookup syntax.
    :param message: Diagnostic text.
    :returns: Diagnostic at the rule name.
    """
    name = tokens(node)[1]
    return Diagnostic(unit.document.path, name.line or 1, name.column or 1, message)


def check_lookups(
    units: list[Unit], entities: EntityIndex, diagnostics: list[Diagnostic]
) -> dict[EntityKey, dict[str, Lookup]]:
    """Resolve rules and construct each entity's inherited lookup view.

    :param units: Compilation units with resolved scopes.
    :param entities: Checked field and inheritance index.
    :param diagnostics: Destination for source diagnostics.
    :returns: Effective rules by candidate entity and rule name.
    """
    local: dict[EntityKey, dict[str, Lookup]] = {}
    analyzer = Computations(entities, diagnostics)
    for unit in units:
        for declaration in [
            *children(unit.document.tree, 'entity'),
            *children(unit.document.tree, 'extension'),
        ]:
            owner = unit.scope.get(declaration_name(declaration))
            if owner is None or owner.kind != 'entity':
                continue
            rules = local.setdefault(owner.key, {})
            for node in children(declaration, 'lookup'):
                name = str(tokens(node)[1])
                if not IDENTIFIER.fullmatch(name):
                    diagnostics.append(
                        name_diagnostic(unit, node, 'lookup names must use snake_case')
                    )
                if name in rules:
                    diagnostics.append(
                        name_diagnostic(unit, node, f'duplicate lookup declaration: {name}')
                    )
                key_nodes = children(node, 'lookup_key')
                if not key_nodes:
                    diagnostics.append(
                        diagnostic(unit.document, node, 'lookup requires at least one key')
                    )
                checker = LookupChecker(analyzer, owner.key, unit)
                environment = {'self': Value(Resolved(owner.key[1], 'reference', key=owner.key))}
                keys: list[LookupKey] = []
                seen: set[str] = set()
                for key_node in key_nodes:
                    checker.descriptor_error = False
                    key_name = str(tokens(key_node)[0])
                    if not IDENTIFIER.fullmatch(key_name):
                        diagnostics.append(
                            diagnostic(
                                unit.document, key_node, 'lookup key names must use snake_case'
                            )
                        )
                    if key_name in seen:
                        diagnostics.append(
                            diagnostic(unit.document, key_node, f'duplicate lookup key: {key_name}')
                        )
                    seen.add(key_name)
                    source = expression(branches(key_node)[0])
                    if not path(source) or source.kind != 'call':
                        checker.issue(
                            source, 'lookup key requires a self field path ending in ::value()'
                        )
                        continue
                    offset = len(checker.accesses)
                    step_offset = len(checker.steps)
                    result = checker.read(checker.infer(source, environment), source)
                    if not supported(result.type, unit):
                        checker.issue(source, 'lookup key requires a supported non-collection type')
                    keys.append(
                        LookupKey(
                            key_name,
                            result.type,
                            checker.elaborate(source),
                            tuple(checker.accesses[offset:]),
                            tuple(
                                checker.finish_step(step) for step in checker.steps[step_offset:]
                            ),
                        )
                    )
                rules[name] = Lookup(
                    owner.key,
                    name,
                    unit,
                    node,
                    declaration,
                    tuple(keys),
                    tuple(checker.inputs[n] for n in sorted(checker.inputs)),
                )
    effective: dict[EntityKey, dict[str, Lookup]] = {}
    for entity in entities.ancestors.keys() | local.keys():
        ancestors = entities.ancestors.get(entity, [])
        merged: dict[str, Lookup] = {}
        for ancestor in reversed(ancestors):
            merged.update(local.get(ancestor, {}))
        for name, rule in local.get(entity, {}).items():
            if name in merged:
                diagnostics.append(
                    name_diagnostic(
                        rule.unit, rule.node, f'lookup redeclares inherited rule: {name}'
                    )
                )
            merged[name] = rule
        effective[entity] = merged
    return effective
