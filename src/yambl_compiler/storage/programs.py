"""Compile checked calculation expressions into ordinary Python functions."""

from collections.abc import Mapping

from yambl_compiler.ir.model import CalculationAccess, Dimension, Field, Model
from yambl_compiler.languages.yambl.computation.records import Expression
from yambl_compiler.storage.conversions import SIGNATURES


class Emitter:
    """Emit vector operations without evaluating or interpreting model source."""

    def __init__(self, model: Model, field: Field) -> None:
        """Retain resolved dependencies for one generated implementation.

        :param model: Checked portable model.
        :param field: Owning implementation.
        :returns: None.
        """
        self.model = model
        self.field = field
        self.accesses = {a.span: a for a in field.calculation.accesses} if field.calculation else {}
        self.references = (
            {t.name: t.value for t in field.calculation.references} if field.calculation else {}
        )
        self.counter = 0

    def fresh(self) -> str:
        """Allocate a generator-owned local name.

        :returns: A collision-free Python identifier.
        """
        self.counter += 1
        return f"v{self.counter}"

    def expression(self, node: Expression, frame: str, locals_: Mapping[str, str]) -> str:
        """Render one checked node as a batch operation.

        :param node: Checked independent expression.
        :param frame: Generated active-frame identifier.
        :param locals_: Source bindings mapped to generator-owned identifiers.
        :returns: Python expression using the shared runtime.
        :raises ValueError: For an unsupported or unresolved node.
        """

        def emit(child: Expression) -> str:
            """Render a child in this lexical context.

            :param child: Child expression.
            :returns: Generated Python expression.
            """
            return self.expression(child, frame, locals_)

        if node.kind in ("string", "integer", "decimal", "bool"):
            literal = {
                "string": repr(node.value),
                "integer": f"int({node.value!r})",
                "decimal": f"Decimal({node.value!r})",
                "bool": str(node.value == "true"),
            }[node.kind]
            hint = node.annotation
            if hint is not None:
                if hint.name == 'code' and hint.arguments:
                    return f'engine.code({frame}, {hint.arguments[0].target or hint.arguments[0].name!r}, {node.value!r})'
                if hint.name in ('date', 'datetime'):
                    literal = f'convert({"parse_" + hint.name!r}, ({node.value!r},))'
                elif hint.name in ('decimal', 'float'):
                    literal = f'{"Decimal" if hint.name == "decimal" else "float"}({node.value!r})'
                elif hint.target and any(
                    d.identity == hint.target and d.kind == 'entity' for d in self.model.definitions
                ):
                    literal = f'EntityRef[object]({hint.target!r}, {literal})'
            return f"engine.literal({frame}, {literal})"
        if node.kind in ('sequence', 'object'):
            hint = node.annotation
            if node.kind == 'sequence':
                return f'engine.construct({frame}, lambda *values: frozenset(values), [{", ".join(emit(n) for n in node.operands)}])'
            if hint is not None and hint.target:
                names = tuple(n.value for n in node.operands[::2])
                values = ', '.join(emit(n) for n in node.operands[1::2])
                return f'engine.construct({frame}, lambda *values: _value_{hint.name}(**dict(zip({names!r}, values, strict=True))), [{values}])'
            return f'engine.construct({frame}, literal_mapping, [{", ".join(emit(n) for n in node.operands)}])'
        if node.kind == "name":
            if node.value == "self":
                return f"{frame}.owners"
            if node.value in locals_:
                return locals_[node.value]
            if node.value in {c.name for c in self.model.constants}:
                return f"engine.status({frame}, {node.value!r})"
            target = self.references.get(node.value)
            definition = next(
                (
                    d
                    for d in self.model.definitions
                    if d.identity == target and d.kind == 'constant'
                ),
                None,
            )
            if definition is not None and definition.initializer is not None:
                prior = self.references
                self.references = {t.name: t.value for t in definition.references}
                result = emit(definition.initializer)
                self.references = prior
                return result
            raise ValueError(f"unresolved generated calculation name: {node.value}")
        access = self.accesses.get(node.span)
        if access is not None:
            selected = node.operands[0].operands[0] if node.kind == "call" else node
            receiver = selected.operands[0]
            args = (
                "{" + ", ".join(f"{a.name!r}: {emit(a.expression)}" for a in node.arguments) + "}"
            )
            return f"engine.field({frame}, {emit(receiver)}, {access.identity!r}, {access.operation!r}, {args})"
        if node.kind == "member":
            receiver = node.operands[0]
            if (
                receiver.kind == 'descriptor'
                and receiver.value == 'subtypes'
                and receiver.operands[0].kind == 'name'
                and receiver.operands[0].value in self.references
            ):
                target = self.references[receiver.operands[0].value] + '::subtypes'
                return f"engine.code({frame}, {target!r}, {node.value!r})"
            if receiver.kind == "name" and receiver.value == "input":
                return f"engine.input({frame}, {node.value!r})"
            if (
                receiver.kind == "name"
                and receiver.value in self.references
                and any(
                    d.identity == self.references[receiver.value] and d.kind == 'decodes'
                    for d in self.model.definitions
                )
            ):
                return f"engine.code({frame}, {self.references[receiver.value]!r}, {node.value!r})"
            if node.value in ("value", "issues"):
                return f"engine.partial_member({frame}, {emit(receiver)}, {node.value!r})"
            if node.annotation and any(
                d.identity == node.annotation.target
                and d.kind == 'value'
                and any(f.name == node.value for f in d.fields)
                for d in self.model.definitions
            ):
                return f'engine.construct({frame}, lambda *values: getattr(values[0], {node.value!r}), [{emit(receiver)}])'
            raise ValueError(f'unresolved field operation: {node.value}')
        if node.kind in ("unary", "binary"):
            if node.value == "??":
                branch = self.fresh()
                return f"engine.default({frame}, {node.operands[0].value!r}, lambda {branch}: {self.expression(node.operands[1], branch, locals_)})"
            if node.value in ("and", "or"):
                branch = self.fresh()
                right = self.expression(node.operands[1], branch, locals_)
                constant = f'engine.literal({branch}, {node.value == "or"})'
                yes, no = (right, constant) if node.value == "and" else (constant, right)
                return f"engine.choose({frame}, {emit(node.operands[0])}, lambda {branch}: {yes}, lambda {branch}: {no})"
            return f'engine.scalar({frame}, {node.value!r}, [{", ".join(emit(e) for e in node.operands)}])'
        if node.kind == "conditional":
            branch = self.fresh()
            alternatives = [self.expression(e, branch, locals_) for e in node.operands[1:]]
            return f"engine.choose({frame}, {emit(node.operands[0])}, lambda {branch}: {alternatives[0]}, lambda {branch}: {alternatives[1]})"
        if node.kind in ("call", "recover"):
            selected = node.operands[0]
            if selected.kind == 'name':
                signature = SIGNATURES[selected.value]
                arguments = {
                    a.name or signature[i][0]: a.expression for i, a in enumerate(node.arguments)
                }
                values = ', '.join(emit(arguments[name]) for name, _ in signature)
                return f'engine.conversion({frame}, {selected.value!r}, [{values}])'
            receiver, method = selected.operands[0], selected.value
            if method == "recover":
                branches: list[str] = []
                for case in node.cases:
                    branch, value = self.fresh(), self.fresh()
                    bindings = {**locals_, case.binding: value} if case.binding else locals_
                    branches.append(
                        f"({case.statuses!r}, lambda {branch}, {value}: {self.expression(case.expression, branch, bindings)})"
                    )
                return f'engine.recover({frame}, {emit(receiver)}, [{", ".join(branches)}])'
            function = node.arguments[0].expression
            branch, value = self.fresh(), self.fresh()
            body = self.expression(function.operands[0], branch, {**locals_, function.value: value})
            return f"engine.sequence({frame}, {emit(receiver)}, {method!r}, lambda {branch}, {value}: {body})"
        raise ValueError(f"unsupported generated calculation node: {node.kind}")

    def fallback_accesses(self, expression: Expression, owner: str) -> None:
        """Resolve configuration references in a checked dimension policy.

        :param expression: Policy expression.
        :param owner: Original dimension owner.
        :returns: None.
        :raises ValueError: If a policy references an unsupported field.
        """
        definitions = {d.identity: d for d in self.model.definitions}
        fields = {f.implementation: f for d in self.model.definitions for f in d.fields}

        def visit(node: Expression) -> str:
            """Resolve scalar reference paths in the policy.

            :param node: Policy node.
            :returns: Referenced entity type, if any.
            """
            if node.kind == "name" and node.value == "self":
                return owner
            if node.kind == "call" and node.operands[0].kind == "descriptor":
                receiver = node.operands[0].operands[0]
                target = visit(receiver)
                access = self.accesses[receiver.span]
                self.accesses[node.span] = CalculationAccess(
                    node.span, access.identity, access.implementation, "value"
                )
                return target
            if node.kind == "member":
                target = visit(node.operands[0])
                definition = definitions.get(target)
                effective = (
                    next((e for e in definition.effective if e.name == node.value), None)
                    if definition
                    else None
                )
                if effective is None:
                    raise ValueError(
                        f"{self.field.identity}: unresolved fallback field {node.value}"
                    )
                field = fields[effective.implementation]
                if field.category not in ("static", "config", "id"):
                    raise ValueError(
                        f"{field.identity}: fallback requires stored static or config dependencies"
                    )
                self.accesses[node.span] = CalculationAccess(
                    node.span, field.identity, field.implementation, "value"
                )
                return field.type.target
            for child in (*node.operands, *(a.expression for a in node.arguments)):
                visit(child)
            return ""

        visit(expression)


def programs(model: Model) -> str:
    """Generate trusted function bodies and a deterministic dispatch registry.

    :param model: Checked model with resolved calculation dependencies.
    :returns: Self-contained package module source.
    :raises ValueError: For unsupported expressions or default cycles.
    """
    lines = [
        '"""Generated batch calculation programs; no source evaluation at runtime."""',
        "from decimal import Decimal",
        "from . import _value_types as value_types",
        "from ._results import EntityRef",
        "from ._conversions import convert",
        "from ._computing import literal_mapping",
        "from ._computing import Computing, Frame, Program, Vector",
        "",
        "",
    ]
    registry: dict[str, str] = {}
    definitions = {d.identity: d for d in model.definitions}

    def function(
        key: str,
        emitter: Emitter,
        expression: Expression,
        bindings: tuple[tuple[str, Expression], ...] = (),
        literal_type: str = '',
    ) -> None:
        """Append one generated function.

        :param key: Runtime dispatch identity.
        :param emitter: Resolved expression context.
        :param expression: Return expression.
        :param bindings: Ordered immutable locals.
        :param literal_type: Contextual type for a declared scalar default.
        :returns: None.
        """
        name = "program_" + str(len(registry))
        registry[key] = name
        lines.extend(
            [
                f"def {name}(engine: Computing, frame: Frame) -> Vector:",
                '    """Evaluate a generated batch.\n\n    :param engine: Shared read session.\n    :param frame: Active contributions.\n    :returns: Aligned results.\n    """',
            ]
        )
        locals_: dict[str, str] = {}
        for source_name, value in bindings:
            local = emitter.fresh()
            lines.append(f'    {local} = {emitter.expression(value, "frame", locals_)}')
            locals_[source_name] = local
        if literal_type == 'date' and expression.kind == 'string':
            lines.append('    from datetime import date')
            emitted = f"engine.literal(frame, date.fromisoformat({expression.value!r}))"
        else:
            emitted = emitter.expression(expression, "frame", locals_)
        lines.extend(["    return " + emitted, "", ""])

    for definition in model.definitions:
        for field in definition.fields:
            calculation = field.calculation
            if calculation is not None:
                emitter = Emitter(model, field)
                pending = {p.name: p.default for p in calculation.inputs if p.default is not None}

                def dependencies(node: Expression) -> set[str]:
                    """Find input dependencies for default scheduling.

                    :param node: Default expression.
                    :returns: Input names.
                    """
                    if (
                        node.kind == "member"
                        and node.operands[0].kind == "name"
                        and node.operands[0].value == "input"
                    ):
                        return {node.value}
                    return set[str]().union(
                        *(dependencies(n) for n in node.operands),
                        *(dependencies(a.expression) for a in node.arguments),
                    )

                while pending:
                    ready = [
                        name
                        for name, default in pending.items()
                        if not dependencies(default).intersection(pending)
                    ]
                    if not ready:
                        raise ValueError(f"{field.identity}: parameter default cycle")
                    for name in ready:
                        function(
                            "default:" + field.implementation + ":" + name,
                            emitter,
                            pending.pop(name),
                            literal_type=next(
                                p.type.name for p in calculation.inputs if p.name == name
                            ),
                        )
                function(
                    field.implementation,
                    emitter,
                    calculation.body.result,
                    tuple((b.name, b.expression) for b in calculation.body.bindings),
                )
            owner = definition.identity
            visible: dict[str, tuple[str, Dimension]] = {}
            while owner in definitions:
                for dimension in definitions[owner].dimensions:
                    visible.setdefault(dimension.name, (owner, dimension))
                owner = definitions[owner].parent
            for call in field.decorators:
                if call.target != "builtins.dimension":
                    continue
                owner, dimension = visible[call.arguments[0].expression.name]
                if dimension.fallback is not None:
                    emitter = Emitter(model, field)
                    emitter.fallback_accesses(dimension.fallback, owner)
                    function(
                        "fallback:" + field.implementation + ":" + dimension.name,
                        emitter,
                        dimension.fallback,
                    )
    lines.append(
        "PROGRAMS: dict[str, Program] = {"
        + ", ".join(f"{key!r}: {name}" for key, name in registry.items())
        + "}"
    )
    if not any("Decimal(" in line for line in lines):
        lines.remove("from decimal import Decimal")
    lines.remove('from . import _value_types as value_types')
    for definition in model.definitions:
        name = definition.name.rsplit('.', 1)[-1]
        if definition.kind == 'value' and any(f'_value_{name}(' in line for line in lines):
            lines.insert(1, f'from ._value_types import {name} as _value_{name}')
    for imported, marker in (
        ('from ._results import EntityRef', 'EntityRef['),
        ('from ._conversions import convert', 'convert('),
        ('from ._computing import literal_mapping', ', literal_mapping,'),
    ):
        if not any(marker in line for line in lines):
            lines.remove(imported)
    if not registry:
        lines[lines.index("from ._computing import Computing, Frame, Program, Vector")] = (
            "from ._computing import Program"
        )
    return "\n".join(lines) + "\n"
