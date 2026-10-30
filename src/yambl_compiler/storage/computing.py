"""Execute generated calculation functions over batches of contribution lanes."""

import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from enum import Enum
from types import MappingProxyType
from typing import cast

from yambl_compiler.ir.model import Type
from yambl_compiler.storage.arithmetic import Number, aggregate, failure, lift, numeric
from yambl_compiler.storage.batch import Batch, FieldRequest
from yambl_compiler.storage.configuration import Configuration
from yambl_compiler.storage.conversions import convert
from yambl_compiler.storage.results import (
    CalculationIssue,
    EntityRef,
    HierarchyView,
    NonNormalResult,
    Partial,
    ReadResult,
    Status,
    Success,
)
from yambl_compiler.storage.scalars import check_finite
from yambl_compiler.storage.selection import Available, BestRequest
from yambl_compiler.storage.session import Session

type Lane = tuple[int, ...]
type Vector = dict[Lane, ReadResult[object]]


def literal_mapping(*values: object) -> Mapping[object, object]:
    """Build an immutable mapping and reject duplicate computed keys.

    :param values: Alternating keys and payloads.
    :returns: Detached mapping.
    :raises ValueError: On duplicate keys.
    """
    result = dict(zip(values[::2], values[1::2], strict=True))
    if len(result) != len(values) // 2:
        raise ValueError('duplicate mapping literal key')
    return MappingProxyType(result)


type Program = Callable[["Computing", "Frame"], Vector]
type Coordinates = tuple[tuple[str, str], ...]
type ConstraintProgram = Callable[
    ["Computing", "Frame"], tuple[tuple[Vector, dict[Lane, Coordinates]], ...]
]
type Lambda = Callable[["Frame", Vector], Vector]


class Omitted(Enum):
    """Distinguish an omitted argument from every user value, including None."""

    VALUE = "omitted"


UNSET = Omitted.VALUE


@dataclass(frozen=True)
class Frame:
    """Retain contribution coordinates and a calculation's local call context.

    :param lanes: Active request and nested collection positions.
    :param owners: Current self references.
    :param inputs: Public inputs already bound in this subtree.
    :param modes: Root-request best-effort choices.
    """

    lanes: tuple[Lane, ...]
    owners: Vector
    inputs: dict[str, Vector]
    modes: tuple[bool, ...]

    def subset(self, lanes: Sequence[Lane]) -> Frame:
        """Retain call context while evaluating a lazy branch.

        :param lanes: Selected contribution coordinates.
        :returns: Restricted frame sharing immutable input results.
        """
        return Frame(tuple(lanes), self.owners, self.inputs, self.modes)


@dataclass(frozen=True)
class Groups:
    """Retain grouped rows until a declared aggregation consumes them.

    :param entries: Keys and their ordered contribution payloads.
    """

    entries: tuple[tuple[object, tuple[tuple[Lane, object], ...]], ...]


def at(vector: Vector, lane: Lane) -> ReadResult[object]:
    """Broadcast an outer local into a nested lambda without duplicating reads.

    :param vector: Results indexed by contribution coordinate.
    :param lane: Current contribution coordinate.
    :returns: Nearest enclosing result.
    """
    while lane not in vector and lane:
        lane = lane[:-1]
    return vector.get(lane, NonNormalResult(Status.ERROR, "missing calculation binding"))


def issues(result: ReadResult[object]) -> tuple[CalculationIssue, ...]:
    """Retain details from a partial payload.

    :param result: Calculation result.
    :returns: Partial issues, empty for normal or failed outcomes.
    """
    return result.issues if isinstance(result, Partial) else ()


def keep_right(left: object, right: object) -> object:
    """Retain the right payload while lift preserves both result histories.

    :param left: Dependency payload already validated by its stage.
    :param right: Selected payload.
    :returns: Right payload.
    """
    return right


class Computing:
    """Evaluate compiled functions while batching their shared field dependencies."""

    def __init__(
        self,
        reader: Session,
        batch: Batch,
        configuration: Configuration,
        programs: Mapping[str, Program],
    ) -> None:
        """Retain generated executable functions and the session's query caches.

        :param reader: Owning read session.
        :param batch: Shared batched field queries.
        :param configuration: Shared effective configuration evaluation.
        :param programs: Trusted functions produced by the compiler, keyed by identity.
        :returns: None.
        """
        self.reader = reader
        self.batch = batch
        self.configuration = configuration
        self.programs = programs
        self.available = Available(batch)
        self.constraint_coordinates: dict[Lane, Coordinates] = {}
        self.trace_constraints = False
        self.computed_fields = {
            field.identity for field in reader.implementations.values() if field.calculation
        }
        self.cache: dict[tuple[object, ...], tuple[Lane, ReadResult[object]]] = {}

    def annotated(
        self,
        result: ReadResult[object],
        frame: Frame,
        source: Vector,
        lane: Lane,
        identity: str,
        arguments: Mapping[str, Vector],
    ) -> ReadResult[object]:
        """Attach a native failure to its instance and calculation coordinates.

        :param result: Read or computation outcome.
        :param frame: Current dependency context.
        :param source: Original scalar receiver references.
        :param lane: Contribution position.
        :param identity: Logical field identity.
        :param arguments: Explicit subtree bindings.
        :returns: Located failure or the original usable result.
        """
        if not isinstance(result, NonNormalResult) or result.issues:
            return result
        owner = at(source, lane)
        payload = owner.value if isinstance(owner, (Success, Partial)) else None
        ref = cast(EntityRef[object], payload) if isinstance(payload, EntityRef) else None
        coordinates: list[tuple[str, str]] = []
        for name, vector in sorted({**frame.inputs, **arguments}.items()):
            value = at(vector, lane)
            if isinstance(value, (Success, Partial)) and value.value is not UNSET:
                coordinates.append((name, str(value.value)))
        issue = CalculationIssue(
            result.status,
            identity,
            ref.target if ref else "",
            str(ref.id) if ref else "",
            tuple(coordinates),
            lane,
            result.message,
        )
        return NonNormalResult(result.status, result.message, (issue,))

    def boundary(self, type_: Type, value: object) -> object:
        """Check declared result constraints without rounding or dropping data.

        :param type_: Declared result contract.
        :param value: Computed payload.
        :returns: Detached, immutable, type-correct payload.
        :raises ValueError: On an incompatible or out-of-range result.
        """
        if type_.target == 'builtins.Hierarchy' and isinstance(value, HierarchyView):
            return value
        definition = next(
            (
                d
                for d in self.reader.layout.model.definitions
                if d.identity == type_.target and d.kind == 'value'
            ),
            None,
        )
        if definition is not None:
            if (
                not is_dataclass(value)
                or type(value).__name__ != definition.name.rsplit('.', 1)[-1]
            ):
                raise ValueError('computed structured value has an incompatible type')
            for field in definition.fields:
                self.boundary(field.type, getattr(value, field.name))
            check_finite(type_, value)
            return value
        if type_.name == "mapping":
            if not isinstance(value, Mapping):
                raise ValueError("computed mapping requires a mapping result")
            return MappingProxyType(
                {
                    self.boundary(type_.arguments[0], k): self.boundary(type_.arguments[1], v)
                    for k, v in cast(Mapping[object, object], value).items()
                }
            )
        if type_.name in ("collection", "values"):
            if not isinstance(value, (tuple, frozenset)):
                raise ValueError("computed collection requires a sequence result")
            return frozenset(
                self.boundary(type_.arguments[0], v)
                for v in cast(tuple[object, ...] | frozenset[object], value)
            )
        if type_.name == "decimal" and type(value) is int:
            value = Decimal(value)
        if type_.name == 'datetime':
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError('computed datetime requires an explicit timezone')
            value = value.astimezone(timezone.utc)
            check_finite(type_, value)
            return value
        if type_.name == "float":
            import math

            if type(value) not in (int, float, Decimal):
                raise ValueError("computed float requires a number")
            value = float(cast(int | float | Decimal, value))
            if not math.isfinite(value):
                raise ValueError("non-finite computed float")
            check_finite(type_, value)
            return value
        self.reader.encode(type_, value)
        return value

    def relocated(
        self, result: ReadResult[object], original: Lane, lane: Lane
    ) -> ReadResult[object]:
        """Attach reused failures to the current contribution rather than the cache owner.

        :param result: Cached outcome.
        :param original: Original call lane.
        :param lane: Current call lane.
        :returns: Outcome with relocated issue paths.
        """
        if isinstance(result, Success):
            return result
        details = tuple(
            (
                replace(i, contribution=lane + i.contribution[len(original) :])
                if i.contribution[: len(original)] == original
                else i
            )
            for i in result.issues
        )
        return replace(result, issues=details)

    def literal(self, frame: Frame, value: object) -> Vector:
        """Broadcast a source literal or immutable constant.

        :param frame: Active contributions.
        :param value: Trusted generated value.
        :returns: Normal results for all active lanes.
        """
        return {lane: Success(value) for lane in frame.lanes}

    def code(self, frame: Frame, target: str, value: str) -> Vector:
        """Construct a checked dictionary code using the generated public type.

        :param frame: Active contributions.
        :param target: Qualified dictionary identity.
        :param value: Checked code name.
        :returns: Typed code results.
        """
        return self.literal(frame, self.reader.code_types.get(target, str)(value))

    def hierarchy_view(self, frame: Frame, source: Vector) -> Vector:
        """Resolve Decodes references as read-only session hierarchy views.

        :param frame: Active contributions.
        :param source: Decodes reference results.
        :returns: Views or the dictionary's non-normal result for each lane.
        """

        def view(value: object) -> HierarchyView:
            """Bind a dictionary identity to a view.

            :param value: Decodes reference.
            :returns: Immutable view identity.
            """
            return HierarchyView(cast(str, cast(EntityRef[object], value).id), self.available)

        def keep_view(value: object, entries: object) -> object:
            """Keep the view while retaining the selected hierarchy outcome.

            :param value: View value.
            :param entries: Validated hierarchy entries.
            :returns: Original view.
            """
            return value

        converted: dict[Lane, ReadResult[object]] = {
            lane: lift(view, [at(source, lane)]) for lane in frame.lanes
        }
        views = [
            result.value for result in converted.values() if not isinstance(result, NonNormalResult)
        ]
        available = self.available.hierarchy(cast(list[HierarchyView], views))
        output: Vector = {}
        for lane, result in converted.items():
            if isinstance(result, NonNormalResult):
                output[lane] = result
            else:
                hierarchy = available[cast(HierarchyView, result.value)]
                output[lane] = lift(keep_view, [result, cast(ReadResult[object], hierarchy)])
        return output

    def status(self, frame: Frame, name: str) -> Vector:
        """Preserve an explicitly returned native non-normal result.

        :param frame: Active contributions.
        :param name: Checked non-normal constant name.
        :returns: Non-normal results for the active lanes.
        """
        return {lane: NonNormalResult(Status(name)) for lane in frame.lanes}

    def input(self, frame: Frame, name: str) -> Vector:
        """Read an explicitly inferred input without data-value fallback.

        :param frame: Current subtree inputs.
        :param name: Public parameter name.
        :returns: Bound values or omission markers.
        """
        vector = frame.inputs.get(name)
        return {
            lane: at(vector, lane) if vector is not None else Success(UNSET) for lane in frame.lanes
        }

    def default(self, frame: Frame, name: str, fallback: Callable[[Frame], Vector]) -> Vector:
        """Evaluate a default only for omitted arguments.

        :param frame: Current subtree context.
        :param name: Parameter receiving a call-omission default.
        :param fallback: Generated lazy default function.
        :returns: Explicit arguments and evaluated defaults.
        """
        given = self.input(frame, name)
        absent = {
            lane
            for lane in frame.lanes
            if isinstance(at(given, lane), Success)
            and cast(Success[object], at(given, lane)).value is UNSET
        }
        replaced = (
            fallback(frame.subset([lane for lane in frame.lanes if lane in absent]))
            if absent
            else {}
        )
        return {
            lane: at(replaced, lane) if lane in absent else at(given, lane) for lane in frame.lanes
        }

    def construct(
        self, frame: Frame, factory: Callable[..., object], operands: Sequence[Vector]
    ) -> Vector:
        """Construct typed immutable values from aligned operands.

        :param frame: Active contributions.
        :param factory: Trusted generated constructor.
        :param operands: Field or element vectors.
        :returns: Constructed values, preserving non-normal results.
        """
        return {lane: lift(factory, [at(v, lane) for v in operands]) for lane in frame.lanes}

    def conversion(self, frame: Frame, name: str, operands: Sequence[Vector]) -> Vector:
        """Convert aligned scalar results while preserving failures and PARTIAL.

        :param frame: Active contributions.
        :param name: Checked builtin name.
        :param operands: Vectors in formal parameter order.
        :returns: Converted vector.
        """
        return {
            lane: lift(lambda *values: convert(name, values), [at(v, lane) for v in operands])
            for lane in frame.lanes
        }

    def scalar(self, frame: Frame, operation: str, operands: Sequence[Vector]) -> Vector:
        """Lift supported scalar operations over aligned result vectors.

        :param frame: Active contributions.
        :param operation: Validated arithmetic, comparison or unary operator.
        :param operands: Already evaluated operand vectors.
        :returns: Result vector with PARTIAL details preserved.
        """

        def apply(*values: object) -> object:
            """Perform one trusted generated operator.

            :param values: Unwrapped typed operands.
            :returns: Scalar result.
            """
            if operation in ("+", "-", "*", '/', '//'):
                if len(values) == 1:
                    return numeric("-", 0, cast(Number, values[0]))
                return numeric(operation, cast(Number, values[0]), cast(Number, values[1]))
            if operation == "not":
                return not values[0]
            comparisons: dict[str, Callable[[object, object], bool]] = {
                "==": operator.eq,
                "!=": operator.ne,
                "<": cast(Callable[[object, object], bool], operator.lt),
                "<=": cast(Callable[[object, object], bool], operator.le),
                ">": cast(Callable[[object, object], bool], operator.gt),
                ">=": cast(Callable[[object, object], bool], operator.ge),
            }
            return comparisons[operation](values[0], values[1])

        return {lane: lift(apply, [at(v, lane) for v in operands]) for lane in frame.lanes}

    def choose(
        self,
        frame: Frame,
        condition: Vector,
        yes: Callable[[Frame], Vector],
        no: Callable[[Frame], Vector],
    ) -> Vector:
        """Evaluate only the selected branch for each contribution.

        :param frame: Active lanes.
        :param condition: Boolean condition outcomes.
        :param yes: Generated true branch.
        :param no: Generated false branch.
        :returns: Branch values preserving condition PARTIAL details.
        """
        output: Vector = {}
        selected: dict[bool, list[Lane]] = {True: [], False: []}
        for lane in frame.lanes:
            result = at(condition, lane)
            if isinstance(result, NonNormalResult):
                output[lane] = result
            elif type(result.value) is not bool:
                output[lane] = NonNormalResult(Status.ERROR, "condition requires bool")
            else:
                selected[result.value].append(lane)
        for truth, branch in ((True, yes), (False, no)):
            lanes = selected[truth]
            if not lanes:
                continue
            values = branch(frame.subset(lanes))
            for lane in lanes:
                output[lane] = lift(keep_right, [at(condition, lane), at(values, lane)])
        return output

    def recover(
        self,
        frame: Frame,
        source: Vector,
        cases: Sequence[tuple[tuple[str, ...], Lambda]],
    ) -> Vector:
        """Evaluate matched recovery branches and leave unmatched results unchanged.

        :param frame: Active contributions.
        :param source: Original outcomes.
        :param cases: Generated lazy branches with statically validated labels.
        :returns: Explicit replacements or unchanged outcomes.
        """
        result = {lane: at(source, lane) for lane in frame.lanes}
        for statuses, branch in cases:
            lanes = [
                lane
                for lane in frame.lanes
                if isinstance(at(source, lane), (NonNormalResult, Partial))
                and cast(NonNormalResult | Partial[object], at(source, lane)).status.value
                in statuses
            ]
            if lanes:
                bound: Vector = {lane: Success(at(source, lane)) for lane in lanes}
                result.update(branch(frame.subset(lanes), bound))
        return result

    def partial_member(self, frame: Frame, bound: Vector, member: str) -> Vector:
        """Expose the typed payload or immutable issues in a PARTIAL case binding.

        :param frame: Matched PARTIAL lanes.
        :param bound: Bound original result objects.
        :param member: value or issues, validated at compile time.
        :returns: Normal values, acknowledging this explicit recovery.
        """
        return {
            lane: Success(getattr(cast(Success[object], at(bound, lane)).value, member))
            for lane in frame.lanes
        }

    def record_member(self, frame: Frame, source: Vector, member: str) -> Vector:
        """Read a record coordinate or propagate its stored value result.

        :param frame: Active contribution lanes.
        :param source: Record values.
        :param member: Checked coordinate or value name.
        :returns: Normal coordinates or the record's native value result.
        """
        output: Vector = {}
        for lane in frame.lanes:
            result = at(source, lane)
            if isinstance(result, NonNormalResult):
                output[lane] = result
                continue
            payload = getattr(result.value, member)
            value = cast(ReadResult[object], payload) if member == 'value' else Success(payload)
            output[lane] = lift(keep_right, [result, value])
        return output

    def _owners(self, frame: Frame, source: Vector) -> Vector:
        """Resolve unique scalar references without constructing entity handles.

        :param frame: Active lanes.
        :param source: Entity reference results.
        :returns: Actual entity identities or preserved non-normal results.
        """
        output: Vector = {}
        references: list[EntityRef[object]] = []
        lanes: list[Lane] = []
        for lane in frame.lanes:
            result = at(source, lane)
            if isinstance(result, NonNormalResult):
                output[lane] = result
                continue
            payload = result.value
            if not isinstance(payload, EntityRef):
                output[lane] = NonNormalResult(
                    Status.ERROR, "field receiver requires an entity reference"
                )
            else:
                references.append(cast(EntityRef[object], payload))
                lanes.append(lane)
        for lane, resolved in zip(lanes, self.batch.entities(references), strict=True):
            output[lane] = lift(keep_right, [at(source, lane), cast(ReadResult[object], resolved)])
        return output

    def field(
        self,
        frame: Frame,
        source: Vector,
        identity: str,
        operation: str,
        arguments: Mapping[str, Vector],
    ) -> Vector:
        """Read one logical field for all active receivers in a single batch plan stage.

        :param frame: Calculation context.
        :param source: Scalar entity references.
        :param identity: Resolved logical field identity.
        :param operation: value, records or best_available.
        :param arguments: Explicit subtree bindings.
        :returns: Aligned field results with contribution locations.
        """
        if identity in self.computed_fields:
            return self.compute(frame, source, identity, arguments)
        owners = self._owners(frame, source)
        output: Vector = {}
        points: list[FieldRequest] = []
        point_lanes: list[Lane] = []
        configs: list[FieldRequest] = []
        config_lanes: list[Lane] = []
        best: list[BestRequest] = []
        best_lanes: list[Lane] = []
        fallback_vectors: dict[str, Vector] = {}
        for lane in frame.lanes:
            owner_result = at(owners, lane)
            if isinstance(owner_result, NonNormalResult):
                output[lane] = owner_result
                continue
            owner = cast(EntityRef[object], owner_result.value)
            field = self.batch.implementation(owner.target, identity)
            if field is None:
                output[lane] = NonNormalResult(Status.NOT_DEFINED)
                continue
            if operation == 'records':
                output[lane] = lift(
                    keep_right, [owner_result, self.reader.records(owner, identity)]
                )
                continue
            parameters = {**frame.inputs, **arguments}
            dims: list[tuple[str, object]] = []
            selectors: list[tuple[str, str | HierarchyView]] = []
            failure_result: NonNormalResult | None = None
            for dimension in self.available.dimensions(field):
                for name, is_selector in (
                    (dimension.value_name, False),
                    (dimension.hierarchy_name, True),
                ):
                    if not name:
                        continue
                    vector = parameters.get(name)
                    result = at(vector, lane) if vector is not None else Success(UNSET)
                    if (
                        is_selector
                        and failure_result is None
                        and not isinstance(result, NonNormalResult)
                        and result.value is UNSET
                        and dimension.hierarchy_default is not None
                        and not any(key == dimension.value_name for key, _ in dims)
                    ):
                        program = (
                            'hierarchy_default:' + field.implementation + ':' + dimension.identity
                        )
                        result = at(
                            self.programs[program](
                                self, Frame((lane,), owners, parameters, frame.modes)
                            ),
                            lane,
                        )
                    if isinstance(result, NonNormalResult):
                        failure_result = result
                        break
                    elif result.value is not UNSET:
                        if is_selector:
                            payload = result.value
                            raw = (
                                cast(EntityRef[object], payload).id
                                if isinstance(payload, EntityRef)
                                else payload
                            )
                            if not isinstance(raw, (str, HierarchyView)):
                                failure_result = NonNormalResult(
                                    Status.ERROR,
                                    "Hierarchy requires a string reference ID",
                                )
                            else:
                                selectors.append((name, raw))
                        else:
                            dims.append((name, result.value))
                if failure_result is not None:
                    break
            if failure_result is not None:
                output[lane] = failure_result
                continue
            when = None
            time_parameters = [
                call.bound[0].key for call in field.decorators if call.target == "builtins.temporal"
            ]
            if field.category in ("snapshot", "scd"):
                for parameter in time_parameters or ["as_of_date"]:
                    vector = parameters.get(parameter)
                    result = at(vector, lane) if vector is not None else Success(UNSET)
                    if isinstance(result, NonNormalResult):
                        failure_result = result
                    elif type(result.value) is not date:
                        failure_result = NonNormalResult(
                            Status.ERROR, f"{parameter} requires a date"
                        )
                    else:
                        when = result.value
            if failure_result is not None:
                output[lane] = failure_result
                continue
            point = FieldRequest(owner, identity, tuple(dims), when)
            if field.category == "config":
                configs.append(point)
                config_lanes.append(lane)
            elif operation == "best_available":
                days = 0
                for dimension in self.available.dimensions(field):
                    if dimension.fallback is None:
                        continue
                    program_key = "fallback:" + field.implementation + ":" + dimension.name
                    if program_key not in fallback_vectors:
                        fallback_vectors[program_key] = self.programs[program_key](
                            self, Frame(frame.lanes, owners, {}, frame.modes)
                        )
                    result = at(fallback_vectors[program_key], lane)
                    if isinstance(result, NonNormalResult):
                        failure_result = result
                    elif type(result.value) is not int:
                        failure_result = NonNormalResult(
                            Status.ERROR, "fallback_days requires integer"
                        )
                    else:
                        days = result.value
                if failure_result is not None:
                    output[lane] = failure_result
                    continue
                best.append(BestRequest(point, tuple(selectors), days))
                best_lanes.append(lane)
            else:
                points.append(point)
                point_lanes.append(lane)
        for lanes, values in (
            (point_lanes, self.batch.read(points)),
            (config_lanes, self.configuration.read(configs)),
            (best_lanes, self.available.read(best)),
        ):
            for lane, result in zip(lanes, values, strict=True):
                if (
                    operation == 'best_available'
                    and lanes is config_lanes
                    and isinstance(result, Success)
                ):
                    result = replace(
                        result, value=self.reader.selection(identity, result.value, {})
                    )
                owner_result = at(owners, lane)
                output[lane] = (
                    result
                    if operation == 'best_available'
                    and isinstance(owner_result, Success)
                    and isinstance(result, Success)
                    else lift(keep_right, [owner_result, result])
                )
        for lane, result in tuple(output.items()):
            output[lane] = self.annotated(result, frame, source, lane, identity, arguments)
        return output

    def compute(
        self,
        frame: Frame,
        source: Vector,
        identity: str,
        arguments: Mapping[str, Vector],
    ) -> Vector:
        """Dispatch actual implementations and normalize inferred input defaults.

        :param frame: Parent call context.
        :param source: Receiver reference vector.
        :param identity: Logical computed field.
        :param arguments: Explicit bindings; other inputs inherit by name.
        :returns: Aligned computation outcomes.
        """
        owners = self._owners(frame, source)
        output: Vector = {}
        groups: dict[str, list[Lane]] = {}
        for lane in frame.lanes:
            result = at(owners, lane)
            if isinstance(result, NonNormalResult):
                output[lane] = result
                continue
            owner = cast(EntityRef[object], result.value)
            field = self.batch.implementation(owner.target, identity)
            if field is None or field.calculation is None:
                output[lane] = NonNormalResult(Status.NOT_DEFINED)
            else:
                groups.setdefault(field.implementation, []).append(lane)
        for implementation, lanes in groups.items():
            field = self.reader.implementations[implementation]
            assert field.calculation is not None
            supplied = {**frame.inputs, **arguments}
            inputs = {
                p.name: supplied.get(p.name, self.literal(frame, UNSET))
                for p in field.calculation.inputs
            }
            child = Frame(tuple(lanes), owners, inputs, frame.modes)
            pending = {
                p.name: p
                for p in field.calculation.inputs
                if p.default is not None and not p.default_dimension
            }
            # Inference has rejected cycles; emitted ordering is topological at generation time.
            for name in self.programs_order(implementation, pending):

                def default_value(context: Frame, parameter_name: str = name) -> Vector:
                    """Evaluate one generated default in its current subtree.

                    :param context: Lanes with an omitted argument.
                    :param parameter_name: Default parameter captured for this invocation.
                    :returns: Default values.
                    """
                    return self.programs["default:" + implementation + ":" + parameter_name](
                        self, context
                    )

                inputs[name] = self.default(child, name, default_value)
            valid: list[Lane] = []
            keys: dict[Lane, tuple[object, ...]] = {}
            for lane in lanes:
                try:
                    parameter_values: list[object] = []
                    for parameter in field.calculation.inputs:
                        result = at(inputs[parameter.name], lane)
                        if isinstance(result, NonNormalResult):
                            output[lane] = result
                            break
                        value = result.value
                        if value is UNSET:
                            if parameter.required and parameter.default is None:
                                raise ValueError(f"missing calculation parameter: {parameter.name}")
                        elif parameter.type.target == 'builtins.Hierarchy' and isinstance(
                            value, HierarchyView
                        ):
                            pass
                        elif parameter.type.kind == "reference" and isinstance(value, str):
                            value = EntityRef[object](parameter.type.target, value)
                            inputs[parameter.name] = {
                                l: at(inputs[parameter.name], l) for l in lanes
                            }
                            inputs[parameter.name][lane] = Success(value)
                            self.reader.encode(parameter.type, value)
                        else:
                            self.reader.encode(parameter.type, value)
                        parameter_values.append((parameter.name, type(value), value))
                    if lane in output:
                        continue
                    key = (
                        cast(Success[object], at(owners, lane)).value,
                        implementation,
                        tuple(parameter_values),
                        frame.modes[lane[0]],
                    )
                    keys[lane] = key
                    if key in self.cache:
                        previous, result = self.cache[key]
                        output[lane] = self.relocated(result, previous, lane)
                    else:
                        valid.append(lane)
                except (ValueError, TypeError, KeyError, DecimalException) as error:
                    output[lane] = NonNormalResult(Status.ERROR, str(error))
            if valid:
                calculated = self.programs[implementation](self, child.subset(valid))
                result_type = field.type

                def checked(value: object) -> object:
                    """Enforce the selected implementation's result boundary.

                    :param value: Usable computed payload.
                    :returns: Checked immutable payload.
                    """
                    return self.boundary(result_type, value)

                for lane in valid:
                    result = at(calculated, lane)
                    result = lift(checked, [result])
                    result = self.annotated(result, child, source, lane, identity, arguments)
                    output[lane] = result
                    if not isinstance(result, NonNormalResult) or result.status != Status.ERROR:
                        self.cache[keys[lane]] = (lane, result)
        return output

    def programs_order(self, implementation: str, pending: Mapping[str, object]) -> tuple[str, ...]:
        """Get emitted default evaluation order from the trusted program registry.

        :param implementation: Computed implementation identity.
        :param pending: Parameters that have defaults.
        :returns: Names in generated dependency order.
        """
        return tuple(
            key.rsplit(":", 1)[-1]
            for key in self.programs
            if key.startswith("default:" + implementation + ":")
            and key.rsplit(":", 1)[-1] in pending
        )

    def sequence(self, frame: Frame, source: Vector, method: str, function: Lambda) -> Vector:
        """Evaluate collection lambdas over one combined dependency batch.

        :param frame: Parent contributions.
        :param source: Collection or grouped results.
        :param method: map, filter, flat_map, group_by, sum_by or all.
        :param function: Generated lambda accepting all selected element lanes.
        :returns: Immutable collections, grouped intermediates or totals.
        """
        if method == 'all':
            return self.sequence_all(frame, source, function)
        elements: Vector = {}
        membership: dict[Lane, list[tuple[Lane, object]]] = {}
        labels: dict[Lane, object] = {}
        output: Vector = {}
        grouped: set[Lane] = set()
        for lane in frame.lanes:
            result = at(source, lane)
            if isinstance(result, NonNormalResult):
                output[lane] = result
                continue
            if isinstance(result.value, Groups):
                grouped.add(lane)
                entries = [
                    (key, coordinate, value)
                    for key, values in result.value.entries
                    for coordinate, value in values
                ]
            else:
                source_payload = result.value
                if isinstance(source_payload, frozenset):
                    values = sorted(
                        cast(frozenset[object], source_payload),
                        key=lambda v: (
                            str(getattr(v, "target", "")),
                            str(getattr(v, "id", v)),
                        ),
                    )
                else:
                    values = list(cast(tuple[object, ...], source_payload))
                entries = [(None, (*lane, i), value) for i, value in enumerate(values)]
            membership[lane] = []
            for label, child, value in entries:
                elements[child] = Success(value)
                labels[child] = label
                membership[lane].append((child, value))
        evaluated = function(frame.subset(tuple(elements)), elements) if elements else {}
        for lane, members in membership.items():
            outcomes = [at(evaluated, child) for child, _ in members]
            failed = [r for r in outcomes if isinstance(r, NonNormalResult)]
            usable = [
                (child, original, at(evaluated, child))
                for child, original in members
                if not isinstance(at(evaluated, child), NonNormalResult)
            ]
            if failed and (
                not frame.modes[lane[0]]
                or not usable
                or any(r.status == Status.ERROR for r in failed)
            ):
                output[lane] = failure(failed)
                continue
            details = issues(at(source, lane)) + tuple(i for r in outcomes for i in issues(r))
            details += tuple(
                i
                for r in failed
                for i in (
                    r.issues or (CalculationIssue(r.status, contribution=lane, message=r.message),)
                )
            )
            payload: object
            if method == "sum_by":
                if lane in grouped:
                    sums: dict[object, list[ReadResult[object]]] = {}
                    for child, _, result in usable:
                        sums.setdefault(labels[child], []).append(result)
                    mapping: dict[object, object] = {}
                    for key, contributions in sums.items():
                        total = aggregate(contributions, frame.modes[lane[0]])
                        if isinstance(total, NonNormalResult):
                            output[lane] = total
                            break
                        mapping[key] = total.value
                    if lane in output:
                        continue
                    payload = MappingProxyType(mapping)
                else:
                    total = aggregate([r for _, _, r in usable], frame.modes[lane[0]])
                    if isinstance(total, NonNormalResult):
                        output[lane] = total
                        continue
                    payload = total.value
            elif method == "group_by":
                partitions: dict[object, list[tuple[Lane, object]]] = {}
                for child, original, result in usable:
                    partitions.setdefault(cast(Success[object], result).value, []).append(
                        (child, original)
                    )
                payload = Groups(tuple((key, tuple(items)) for key, items in partitions.items()))
            elif method == "filter":
                payload = tuple(
                    original
                    for _, original, result in usable
                    if cast(Success[object], result).value
                )
            elif method == "flat_map":
                payload = tuple(
                    item
                    for _, _, result in usable
                    for item in cast(Sequence[object], cast(Success[object], result).value)
                )
            else:
                payload = tuple(cast(Success[object], result).value for _, _, result in usable)
            output[lane] = Partial(payload, details) if details else Success(payload, "computation")
        return output

    def sequence_all(self, frame: Frame, source: Vector, function: Lambda) -> Vector:
        """Evaluate each predicate in order until false or a non-normal result.

        :param frame: Parent contributions.
        :param source: Collection results.
        :param function: Generated predicate.
        :returns: One short-circuited boolean result per parent lane.
        """
        output: Vector = {}
        for lane in frame.lanes:
            result = at(source, lane)
            if isinstance(result, NonNormalResult):
                output[lane] = result
                continue
            payload = result.value
            values = (
                sorted(
                    payload, key=lambda v: (str(getattr(v, 'target', '')), str(getattr(v, 'id', v)))
                )
                if isinstance(payload, frozenset)
                else payload
            )
            for index, value in enumerate(cast(Sequence[object], values)):
                child = (*lane, index)
                predicate = at(function(frame.subset((child,)), {child: Success(value)}), child)
                if (
                    self.trace_constraints
                    and (not isinstance(predicate, Success) or predicate.value is False)
                    and is_dataclass(value)
                    and not isinstance(value, type)
                    and hasattr(value, 'value')
                ):
                    self.constraint_coordinates[lane] = tuple(
                        (member.name, str(getattr(value, member.name)))
                        for member in fields(value)
                        if member.name != 'value'
                    )
                if not isinstance(predicate, Success):
                    output[lane] = predicate
                    break
                if type(predicate.value) is not bool:
                    output[lane] = NonNormalResult(Status.ERROR, 'all predicate requires bool')
                    break
                if not predicate.value:
                    output[lane] = (
                        Partial(False, result.issues)
                        if isinstance(result, Partial)
                        else Success(False, 'computation')
                    )
                    break
            else:
                output[lane] = (
                    Partial(True, result.issues)
                    if isinstance(result, Partial)
                    else Success(True, 'computation')
                )
        return output
