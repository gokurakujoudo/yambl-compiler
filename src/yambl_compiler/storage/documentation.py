"""Render model metadata and Python API contracts as safe rST docstrings."""

from textwrap import fill

from yambl_compiler.ir.model import Call, Definition, Field
from yambl_compiler.storage.layout import Layout, StoredField, type_label


def docstring(lines: list[str], indent: int) -> list[str]:
    """Quote an rST document as a multiline Python string literal.

    :param lines: Nonempty document, including intentional blank lines.
    :param indent: Spaces before the Python statement and its content.
    :returns: Source lines preserving quotes, backslashes and Unicode at runtime.
    """
    wrapped = [
        (
            fill(
                line,
                width=100 - indent,
                subsequent_indent="    ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            if line.startswith((":param ", ":returns:"))
            else line
        )
        for line in lines
    ]
    escaped = (
        "\n".join(wrapped)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\x00", "\\x00")
    )
    parts = escaped.split("\n")
    pad = " " * indent
    return [
        pad + '"""' + parts[0],
        *(pad + line if line else "" for line in parts[1:]),
        pad + '"""',
    ]


def literal_section(label: str, text: str) -> list[str]:
    """Keep model prose and type expressions out of rST markup interpretation.

    :param label: Generator-owned section label.
    :param text: Model text, which may contain rST directives or punctuation.
    :returns: Literal block retaining the text and its line structure.
    """
    return [label + "::", "", *("    " + line for line in text.split("\n")), ""]


def metadata(calls: tuple[Call, ...]) -> list[str]:
    """Describe optional title and description using resolved decorator identities.

    :param calls: Bound metadata from the declaration.
    :returns: Literal sections for present, nonempty metadata.
    """
    lines: list[str] = []
    for name in ("title", "description"):
        for call in calls:
            if call.target == "builtins." + name:
                value = call.bound[0].type.exact
                if value is not None and value.text.strip():
                    lines.extend(literal_section(name.capitalize(), value.text))
    return lines


def entity_documentation(entity: Definition) -> list[str]:
    """Document a generated entity handle with its own declaration metadata.

    :param entity: Resolved entity declaration, without inferred inherited metadata.
    :returns: Entity docstring source lines.
    """
    return docstring(
        [
            f"Read handle for {entity.identity}.",
            "",
            *metadata(entity.decorators),
            "Properties lazily cache complete immutable field data. Read methods cache normalized queries.",
            "Cached data remains usable after closing the session; additional reads return ERROR.",
        ],
        4,
    )


def field_documentation(
    layout: Layout,
    stored: StoredField,
    output: str,
    time_key: str,
    interval: bool = False,
    property_: bool = False,
) -> list[str]:
    """Document a logical field, all declaration owners and its read contract.

    :param layout: Complete embedded model and physical field mappings.
    :param stored: Logical field represented by the generated method.
    :param output: Generated Python payload type annotation.
    :param time_key: Point-query time parameter, empty for time-independent fields.
    :param interval: Whether this method reads a half-open date interval.
    :param property_: Whether this is a complete lazy property.
    :returns: Indented rST docstring source lines for a generated read method.
    """
    field = stored.field
    logical = next(f for f in layout.model.fields if f.identity == field.identity)
    owners = sorted(
        {implementation.rsplit(".", 1)[0] for implementation in logical.implementations}
    )
    lines = [
        f"Read {field.identity}" + (" in [start_date, end_date)." if interval else "."),
        "",
    ]
    lines.extend(metadata(field.decorators))
    lines.extend(literal_section("YAMBL type", type_label(logical.type)))
    lines.extend(literal_section("Declaring entities", "\n".join(owners)))
    lines.extend(dimension_documentation(field))
    lines.extend(
        [
            "Queries are cached by normalized parameters. A complete property cache also serves point and range queries.",
            "Cached data remains available after close. ERROR is not cached. Range lists are detached copies.",
            "",
        ]
    )
    if property_:
        lines.extend(
            [
                ":returns: Immutable complete field data, loaded on first access; empty coordinate sets succeed with an empty tuple. Invalid coordinates return outer ERROR; payload errors stay inside records. Inapplicable fields return NOT_DEFINED.",
                f":rtype: ReadResult[{output}]",
            ]
        )
        return docstring(lines, 8)
    if interval:
        lines.extend(
            [
                ":param start_date: Inclusive interval start.",
                ":type start_date: date",
                ":param end_date: Exclusive interval end; equal bounds return an empty list.",
                ":type end_date: date",
            ]
        )
    elif time_key:
        meaning = (
            "Exact snapshot date."
            if field.category == "snapshot"
            else "Date within a [valid_from, valid_to) period."
        )
        lines.extend([f":param {time_key}: {meaning}", f":type {time_key}: date"])
    for dimension in stored.dimensions:
        lines.append(
            f":param {dimension.key}: Required dimension value used for exact matching; see Dimensions above."
        )
    if interval:
        behavior = (
            "Existing snapshot dates only."
            if field.category == "snapshot"
            else "State at the start followed by period boundaries; gaps are NO_ENTRY."
        )
        lines.append(
            ":returns: Success containing ordered (date, ReadResult) pairs. "
            + behavior
            + " Per-record errors stay in the list; whole-query failures and reversed bounds return outer ERROR. "
            + "An inapplicable field returns NOT_DEFINED."
        )
        result = f"ReadResult[list[tuple[date, ReadResult[{output}]]]]"
    else:
        lines.append(
            ":returns: Success containing the typed value, or a non-normal result. "
            "An inapplicable field returns NOT_DEFINED; invalid parameters or read failures return ERROR."
        )
        if field.category == "config":
            lines[
                -1
            ] += " NO_ENTRY uses the actual implementation default when present, with origin model_default; EMPTY and NOT_APPLICABLE do not."
        result = f"ReadResult[{output}]"
    lines.append(f":rtype: {result}")
    return docstring(lines, 8)


def dimension_documentation(field: Field) -> list[str]:
    """Describe dimensions and how selectors relate to exact reads.

    :param field: Field declaration containing bound dimension decorators.
    :returns: One literal section, or no section when no dimensions exist.
    """
    dimensions: list[tuple[str, str]] = []
    for call in field.decorators:
        if call.target == "builtins.dimension":
            variation = next(b for b in call.bound if b.name == "variation")
            text = f"{variation.key}: {type_label(variation.type)} (required explicitly)"
            for selector in call.bound:
                if selector.name == "selector":
                    text += f"\n    Hierarchy selector {selector.key}: {type_label(selector.type)} (used by best-available selection; exact reads do not apply it)"
            dimensions.append((variation.key, text))
    return (
        literal_section("Dimensions", "\n".join(text for _, text in sorted(dimensions)))
        if dimensions
        else []
    )
