"""Typed records forming the experimental JSON IR contract."""

from dataclasses import dataclass
from typing import Literal

from yambl_compiler.computation.records import (
    Body,
    Span,
)
from yambl_compiler.computation.records import (
    Expression as CalculationExpression,
)
from yambl_compiler.computation.records import (
    Record as Record,
)


@dataclass(frozen=True)
class Source(Record):
    """Locate a definition without recording a checkout directory.

    :param origin: Model repository or bundled language definition.
    :param path: Relative POSIX path.
    :param namespace: Complete lexical namespace, including repository prefix.
    :param line: One-based starting line.
    :param column: One-based starting Unicode code-point column.
    :param end_line: One-based ending line.
    :param end_column: Exclusive ending column.
    """

    origin: Literal['model', 'builtin']
    path: str
    namespace: str
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class Text(Record):
    """Associate a name with a textual value.

    :param name: Parameter, primitive or constant name.
    :param value: Exact textual content.
    """

    name: str
    value: str


@dataclass(frozen=True)
class Value(Record):
    """Preserve scalar and identity values without JSON numeric rounding.

    :param kind: Value category.
    :param text: Normalized scalar value or reference ID.
    :param target: Qualified type or dictionary identity.
    :param selections: Canonical selected flags codes.
    :param mask: Decimal flags bit mask, empty for other values.
    """

    kind: Literal[
        'string',
        'integer',
        'decimal',
        'bool',
        'reference',
        'type',
        'dictionary',
        'code',
        'flags',
    ]
    text: str
    target: str
    selections: tuple[str, ...] = ()
    mask: str = ''


@dataclass(frozen=True)
class Access(Record):
    """Select a field or descriptor member.

    :param operator: Field or descriptor access operator.
    :param name: Selected member.
    :param target: Logical field identity when statically determined.
    :param source: Original selection location.
    """

    operator: Literal['.', '::']
    name: str
    target: str
    source: Source


@dataclass(frozen=True)
class Operation(Record):
    """Retain a symbolic flags operation.

    :param operator: Union or removal.
    :param operand: Right-hand expression.
    """

    operator: Literal['+', '-']
    operand: Expression


@dataclass(frozen=True)
class Expression(Record):
    """Describe a resolved lexical expression and its explicit bindings.

    :param kind: Root interpretation.
    :param name: Lexical primitive, parameter or short declaration name.
    :param target: Qualified declaration or external dictionary identity.
    :param literal: Literal value, if present.
    :param arguments: Ordered generic arguments.
    :param parameters: Named normalized primitive parameters.
    :param accesses: Ordered descriptor selections.
    :param operations: Symbolic flags operations in evaluation order.
    :param binding: Locally introduced mapping binding.
    :param source: Original expression location.
    """

    kind: Literal['primitive', 'generic', 'reference', 'parameter', 'self', 'literal', 'external']
    name: str
    target: str
    literal: Value | None
    arguments: tuple[Expression, ...]
    parameters: tuple[Text, ...]
    accesses: tuple[Access, ...]
    operations: tuple[Operation, ...]
    binding: str
    source: Source


@dataclass(frozen=True)
class Type(Record):
    """Describe a checked type alongside its structured declaring expression.

    :param name: Primitive or short declared type name.
    :param kind: Semantic storage category.
    :param target: Qualified declaration identity when applicable.
    :param arguments: Resolved type arguments.
    :param parameters: Normalized primitive parameters.
    :param exact: Singleton value constraint.
    :param dependent: Whether the declaring expression depends on an instance or binding.
    :param members: Candidate contracts selected by attrs.
    :param candidates: Logical field identities selectable by attrs.
    """

    name: str
    kind: Literal['value', 'string', 'reference', 'collection', 'unknown']
    target: str
    arguments: tuple[Type, ...]
    parameters: tuple[Text, ...]
    exact: Value | None
    dependent: bool
    members: tuple[Type, ...]
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class Argument(Record):
    """Retain an invocation argument and its source form.

    :param kind: Positional value, named value or key/type pair.
    :param name: Named argument or caller-defined pair key.
    :param expression: Argument expression.
    :param default: Optional caller-defined input default on a pair.
    """

    kind: Literal['positional', 'keyword', 'pair']
    name: str
    expression: Expression
    default: Expression | None = None


@dataclass(frozen=True)
class Bound(Record):
    """Expose a checked decorator binding with defaults filled.

    :param name: Formal parameter name.
    :param key: Caller-defined pair key, empty for ordinary arguments.
    :param type: Resolved value or pair type.
    """

    name: str
    key: str
    type: Type


@dataclass(frozen=True)
class Call(Record):
    """Preserve one decorator application, including identical repeated calls.

    :param target: Qualified decorator identity.
    :param arguments: Invocation arguments in source order.
    :param bound: Bound parameters in formal order.
    :param source: Application location.
    """

    target: str
    arguments: tuple[Argument, ...]
    bound: tuple[Bound, ...]
    source: Source


@dataclass(frozen=True)
class Formal(Record):
    """Describe an alias or decorator parameter.

    :param name: Parameter name.
    :param type: Constraint expression.
    :param default: Default expression for an ordinary decorator parameter.
    :param pair: Whether this parameter accepts a new key/type pair.
    :param optional: Whether the pair may be omitted.
    :param source: Parameter location.
    """

    name: str
    type: Expression
    default: Expression | None
    pair: bool
    optional: bool
    source: Source


@dataclass(frozen=True)
class Entry(Record):
    """Represent a declared or generated dictionary entry.

    :param code: Dictionary-local code.
    :param ordering: Decimal ordering or flags mask.
    :param decode: Display text.
    :param generated: Whether this is automatic None or All.
    :param source: Entry or generating dictionary location.
    """

    code: str
    ordering: str
    decode: str
    generated: bool
    source: Source


@dataclass(frozen=True)
class Field(Record):
    """Separate field identity, implementation and declaring context.

    :param name: Local field name.
    :param identity: Logical field identity shared by related implementations.
    :param implementation: Qualified owner and local field name.
    :param category: ID or attribute category.
    :param expression: Resolved lexical type expression, preserving alias use.
    :param expanded: Alias-expanded expression with instance dependencies intact.
    :param type: Effective checked type.
    :param default: Configuration default, if present.
    :param decorators: Metadata applications.
    :param extension: Whether the field originated in an extend block.
    :param source: Original declaration location.
    :param calculation: Checked computation, absent for stored fields.
    """

    name: str
    identity: str
    implementation: str
    category: Literal['id', 'static', 'config', 'snapshot', 'scd', "computed"]
    expression: Expression
    expanded: Expression
    type: Type
    default: Value | None
    decorators: tuple[Call, ...]
    extension: bool
    source: Source
    calculation: Calculation | None = None


@dataclass(frozen=True)
class CalculationInput(Record):
    """Expose one inferred calculation argument.

    :param name: Public parameter name.
    :param type: Resolved input constraint.
    :param required: Whether an unbound input requires a caller value.
    :param default: Optional input-dependent or literal default.
    :param sites: Contributing expression locations.
    """

    name: str
    type: Type
    required: bool
    default: CalculationExpression | None
    sites: tuple[Source, ...]


@dataclass(frozen=True)
class CalculationAccess(Record):
    """Resolve a field operation without importing source files at runtime.

    :param span: Field access location.
    :param identity: Logical field identity.
    :param implementation: Original declaring implementation.
    :param operation: Requested field operation.
    """

    span: Span
    identity: str
    implementation: str
    operation: Literal["best_available", "value"]


@dataclass(frozen=True)
class Calculation(Record):
    """Carry a checked calculation body and its dependency contract.

    :param body: Structured expression body.
    :param inputs: Inferred public arguments, sorted by name.
    :param accesses: Resolved field operations.
    :param dependencies: Referenced computed implementation identities.
    :param references: Used lexical declaration names and resolved identities.
    """

    body: Body
    inputs: tuple[CalculationInput, ...]
    accesses: tuple[CalculationAccess, ...]
    dependencies: tuple[str, ...]
    references: tuple[Text, ...]


@dataclass(frozen=True)
class Dimension(Record):
    """Define an entity-scoped dimension and its selection policy.

    :param name: Entity-local dimension name.
    :param value_name: Explicit dimension parameter name.
    :param expression: Lexical value type expression.
    :param type: Resolved dimension value contract.
    :param hierarchy_name: Selector parameter, empty when absent.
    :param hierarchy_default: Optional selector reference ID.
    :param fallback: Per-instance fallback expression, or None.
    :param source: Original declaration location.
    """

    name: str
    value_name: str
    expression: Expression
    type: Type
    hierarchy_name: str
    hierarchy_default: CalculationExpression | None
    fallback: CalculationExpression | None
    source: Source


@dataclass(frozen=True)
class Effective(Record):
    """Select an implementation for one actual entity type.

    :param name: Visible field name.
    :param implementation: Implementing declaration identity.
    :param identity: Logical field identity.
    """

    name: str
    implementation: str
    identity: str


@dataclass(frozen=True)
class Definition(Record):
    """Preserve a declaration and its checked effective view.

    :param kind: Declaration kind.
    :param name: Qualified declaration name.
    :param identity: Reference key; model:: distinguishes a local builtins namespace.
    :param final: Whether inheritance or extension is forbidden.
    :param parent: Explicit parent identity, empty when absent.
    :param parameters: Generic or decorator parameters.
    :param expression: Alias body, absent for other declarations.
    :param decorators: Applied metadata.
    :param fields: Own fields, including merged extensions.
    :param effective: Effective implementation choices, including inherited fields.
    :param entries: Declared and generated dictionary entries.
    :param source: Original declaration location.
    :param dimensions: Entity-owned dimension declarations, including extensions.
    :param initializer: Checked pure constant expression, absent on other definitions.
    :param references: Lexical identities available to the constant initializer.
    """

    kind: Literal['entity', 'value', 'type_alias', 'decorator', 'decodes', 'constant']
    name: str
    identity: str
    final: bool
    parent: str
    parameters: tuple[Formal, ...]
    expression: Expression | None
    decorators: tuple[Call, ...]
    fields: tuple[Field, ...]
    effective: tuple[Effective, ...]
    entries: tuple[Entry, ...]
    source: Source
    dimensions: tuple[Dimension, ...] = ()
    initializer: CalculationExpression | None = None
    references: tuple[Text, ...] = ()


@dataclass(frozen=True)
class LogicalField(Record):
    """Expose a field's checked contract and complete applicability table.

    :param identity: Shared qualified logical field identity.
    :param type: Common checked contract, including overload reconciliation.
    :param implementations: Every declaring implementation identity.
    :param applicable: Entity or value identities where the field is defined.
    """

    identity: str
    type: Type
    implementations: tuple[str, ...]
    applicable: tuple[str, ...]


@dataclass(frozen=True)
class External(Record):
    """Track an explicitly external dictionary without fetching data.

    :param name: Qualified dictionary identity in its lexical namespace.
    :param sites: All reference locations.
    """

    name: str
    sites: tuple[Source, ...]


@dataclass(frozen=True)
class Model(Record):
    """Carry a complete experimental intermediate model.

    :param format_revision: Supported wire-format revision.
    :param compiler_version: Producing compiler version.
    :param prefix: Source repository prefix.
    :param primitives: Primitive names and categories.
    :param constants: Result constant names and meanings.
    :param definitions: User and embedded built-in declarations.
    :param fields: Logical contracts, implementations and applicable definitions.
    :param external_dictionaries: Explicit external dependencies.
    :param builtins_digest: SHA-256 of embedded built-in semantics.
    :param model_digest: SHA-256 excluding source positions and producer version.
    """

    format_revision: int
    compiler_version: str
    prefix: str
    primitives: tuple[Text, ...]
    constants: tuple[Text, ...]
    definitions: tuple[Definition, ...]
    fields: tuple[LogicalField, ...]
    external_dictionaries: tuple[External, ...]
    builtins_digest: str
    model_digest: str
