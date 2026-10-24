"""Exercise decorator binding, captured definitions and metadata contracts."""

import unittest

from tests.support import document
from yambl_compiler.formatting.formatter import format_document
from yambl_compiler.semantics.validation import check

VALID = [
    "@targets(DecoratorTargets.All) decorator @tag(text: string['[A-Z]+'] = 'ABC', amount: decimal[18, 4] = 2, rate: float = 1.2, count: integer = 1, active: bool = true, key: snowflake_id = 123) @tag entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(text: string) @tag('hello') entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(text: string = 'hello', rank: integer) @tag(rank=2) entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(text: string, rank: integer) @tag(rank=2, text='hello') entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(text: string, rank: integer) @tag('hello', rank=2) entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(*source: string, *selector?: Hierarchy) @tag(source: code<'External'>) entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(*source: string, *selector?: Hierarchy) @tag(source: string, fallback: Hierarchy) entity A {}",
    '@targets(DecoratorTargets.All) decorator @tag() @tag entity A {} @tag() value item { @tag attr item: string }',
    "@title(text='Tag') @description('Metadata') @allow_multiple @targets(DecoratorTargets.All) decorator @tag(text: string) @tag('a') @tag('a') entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag() @tag @targets(DecoratorTargets.All) decorator @other() @other type label = string",
    "@flags() decodes Codes {}",
    "entity A { @temporal(effective_date: date) snapshot attr x: string }",
    "@targets(DecoratorTargets.All) decorator @proxy(target: type<self::entity>) entity Security {} entity Holding { @proxy(Holding) attr security: Security }",
    "@targets(DecoratorTargets.All) decorator @proxy(target: type<self::entity>) entity Holding {} entity Child inherits Holding {} extend Holding { @proxy(Child) attr x: string }",
    "@targets(DecoratorTargets.All) decorator @proxy(target: type<self>) @proxy(Child) entity Holding {} entity Child inherits Holding {}",
    "@targets(DecoratorTargets.All) decorator @tag(text: self::type) entity A { @tag('hello') attr name: string }",
    "@targets(DecoratorTargets.All) decorator @tag(text: self::type = 'hello') entity A { @tag attr name: string }",
    "@targets(DecoratorTargets.All) decorator @tag(target: type<Entity> = self::entity) entity A { @tag attr name: string }",
    "entity Security { id key: string['[A-Z]+'] } @targets(DecoratorTargets.All) decorator @tag(target: Security) @tag('ABC') entity A {}",
    "entity Security { id key: snowflake_id } entity Bond inherits Security {} @targets(DecoratorTargets.All) decorator @tag(target: Bond = '123') @tag entity A {}",
    "decodes Codes { - 1 One: 'One' } @targets(DecoratorTargets.All) decorator @tag(dict: Decodes, code: code<Codes>, target: type<Entity>) @tag(Codes, Codes.One, A) entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(flags: code<AttrCats> = AttrCats.All - AttrCats.Static) @tag(AttrCats.Config + AttrCats.Static) entity A {}",
    "@targets(DecoratorTargets.All) decorator @tag(text: string = 'x') entity A { @tag attr x: string } entity B inherits A { @override @tag(text='x') attr x: string }",
    "@allow_multiple @targets(DecoratorTargets.All) decorator @tag(text: string) entity A {} entity B inherits A { @overload @tag('a') @tag('b') attr x: string } entity C inherits A { @tag('b') @overload @tag('a') attr x: string }",
    "@targets(DecoratorTargets.All) decorator @tag() @tag decodes Codes {} @tag value item { @tag attr x: string } @tag type label = string entity A { @tag id key: string }",
    [
        "namespace meta type text = string @targets(DecoratorTargets.All) decorator @tag(label: text = 'x')",
        "namespace app from meta import tag @tag entity A {}",
    ],
    [
        "namespace meta @targets(DecoratorTargets.All) decorator @tag(target: type<self::entity>)",
        "namespace app from meta import tag entity A { @tag(A) attr x: string }",
    ],
    "@tag entity A {} @targets(DecoratorTargets.All) decorator @tag()",
]

INVALID = [
    (
        'entity A { @dimension(source: Hierarchy) attr x: string }',
        'dimension value must be string or code',
    ),
    ('entity A { @dimension(source: Missing) attr x: string }', 'unknown type: Missing'),
    ('entity A { @dimension() attr x: string }', 'missing decorator argument: variation'),
    (
        'namespace builtins @allow_multiple @allow_multiple @targets(DecoratorTargets.All) decorator @allow_multiple()',
        'duplicate decorator: allow_multiple',
    ),
    ('@targets(DecoratorTargets.All) decorator @Bad()', 'decorator names must use snake_case'),
    (
        '@targets(DecoratorTargets.All) decorator @tag(Bad: string)',
        'decorator parameter names must use snake_case',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: string, a: string)',
        'duplicate decorator parameter',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(*a: string, b: string)',
        'ordinary parameters precede pairs',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(*a?: string, *b: string)',
        'optional pairs come last',
    ),
    ('@targets(DecoratorTargets.All) decorator @tag(a?: string)', 'only pairs use ?'),
    (
        "@targets(DecoratorTargets.All) decorator @tag(*a: string = 'x')",
        'only ordinary parameters use defaults',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: values<string>)',
        'decorator parameters cannot be collections',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag(a: integer = 'x')",
        'incompatible decorator argument',
    ),
    ('@targets(DecoratorTargets.All) decorator @tag(a: Missing)', 'unknown type: Missing'),
    ('@missing entity A {}', 'unknown decorator: missing'),
    ('type tag = string @tag entity A {}', 'unknown decorator: tag'),
    (
        '@targets(DecoratorTargets.All) decorator @tag() entity A { attr x: tag }',
        'unknown type: tag',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag(a: string) @tag entity A {}",
        'missing decorator argument: a',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag(a: string) @tag(a='x', 'y') entity A {}",
        'positional arguments must precede named arguments',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag(a: string) @tag('x', a='y') entity A {}",
        'duplicate decorator argument: a',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag(a: string) @tag(unknown='x') entity A {}",
        'unexpected argument for decorator',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag() @tag('x') entity A {}",
        'unexpected argument for decorator',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag() @tag(x: string) entity A {}',
        'unexpected argument for decorator',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag(a: string, *p: string) @tag(x: string, a='x') entity A {}",
        'new pairs must follow ordinary arguments',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(*p: string) @tag(Bad: string) entity A {}',
        'pair keys must use snake_case',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(*p: string, *q: string) @tag(x: string, x: string) entity A {}',
        'duplicate pair key: x',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(*p: string) @tag(x: integer) entity A {}',
        'incompatible decorator pair type',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(*p: string) @tag(x: values<string>) entity A {}',
        'incompatible decorator pair type',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: integer) @tag(string) entity A {}',
        'incompatible decorator argument',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: self::type) entity A { @tag attr x: values<string> }',
        'decorator parameters cannot be collections',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag() @tag @tag entity A {}',
        'duplicate decorator: tag',
    ),
    ('@allow_multiple entity A {}', 'decorator allow_multiple does not allow this target'),
    (
        '@allow_multiple @allow_multiple @targets(DecoratorTargets.All) decorator @tag()',
        'duplicate decorator: allow_multiple',
    ),
    ('@flags type label = string', 'decorator flags does not allow this target'),
    ('entity A { @flags attr x: string }', 'decorator flags does not allow this target'),
    ('@override entity A {}', 'decorator override does not allow this target'),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: type<self::entity>) @tag(A) entity A {}',
        ':: requires a field description',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: type<self::entity>) value item { @tag(A) attr x: string } entity A {}',
        'field description has no owning entity',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: type<self::entity>) entity A { @tag(B) attr x: string } entity B {}',
        'incompatible decorator argument',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag(a: self::value) entity A { @tag attr x: string }',
        'field value requires an instance origin',
    ),
    (
        "entity A {} @targets(DecoratorTargets.All) decorator @tag(a: A) @tag('id') entity B {}",
        'reference argument requires one identifiable ID field',
    ),
    (
        "entity A { id one: string id two: string } @targets(DecoratorTargets.All) decorator @tag(a: A) @tag('id') entity B {}",
        'reference argument requires one identifiable ID field',
    ),
    (
        "entity A { id key: integer } @targets(DecoratorTargets.All) decorator @tag(a: A) @tag('id') entity B {}",
        'incompatible decorator argument',
    ),
    (
        "@targets(DecoratorTargets.All) decorator @tag(a: string) entity A { @tag('x') attr x: string } entity B inherits A { @override @tag('y') attr x: string }",
        'override signature mismatch',
    ),
    (
        "@allow_multiple @targets(DecoratorTargets.All) decorator @tag() entity A { @tag @tag attr x: string } entity B inherits A { @override @tag attr x: string }",
        'override signature mismatch',
    ),
    (
        '@targets(DecoratorTargets.All) decorator @tag() @tag extend A {} entity A {}',
        'invalid YAMBL syntax',
    ),
]


TARGET_SITES = {
    'Entity': '@tag entity A {}',
    'Value': '@tag value item {}',
    'Decodes': '@tag decodes Codes {}',
    'Type': '@tag type label = string',
    'Decorator': '@tag @targets(DecoratorTargets.None) decorator @other()',
    'Id': 'entity A { @tag id key: string }',
    'EntityAttr': 'entity A { @tag attr item: string }',
    'ValueAttr': 'value item { @tag attr item: string }',
}
VALID.extend(
    f'@targets(DecoratorTargets.{kind}) decorator @tag() {site}'
    for kind, site in TARGET_SITES.items()
)
INVALID.extend(
    (
        f'@targets(DecoratorTargets.{kind}) decorator @tag() {site}',
        'decorator tag does not allow this target',
    )
    for kind in TARGET_SITES
    for other, site in TARGET_SITES.items()
    if kind != other
)
VALID.extend(
    [
        '@targets(DecoratorTargets.None) decorator @unused()',
        '@targets(DecoratorTargets.EntityAttr, categories=AttrCats.Config + AttrCats.Snapshot) decorator @tag() entity A { @tag config attr x: integer = 1 @tag snapshot attr y: values<string> } extend A { @tag snapshot attr z: string }',
        '@targets(DecoratorTargets.ValueAttr, categories=AttrCats.Static) decorator @tag() value item { @tag attr implicit: string @tag static attr explicit: string }',
        '@targets(categories=AttrCats.None, targets=DecoratorTargets.Id + DecoratorTargets.Entity) decorator @tag() @tag entity A { @tag id key: string }',
        '@targets(DecoratorTargets.All - DecoratorTargets.EntityAttr - DecoratorTargets.EntityAttr + DecoratorTargets.Type + DecoratorTargets.Type) decorator @tag() @tag type label = string',
        'namespace builtins @targets(DecoratorTargets.Entity) decorator @flags() @flags entity A {}',
        [
            'namespace usage from meta import tag @tag entity A {}',
            'namespace meta @targets(DecoratorTargets.Entity) decorator @tag()',
        ],
    ]
)
INVALID.extend(
    [
        (
            'namespace builtins @targets() decorator @targets() @targets() decorator @tag() @tag entity A { @tag attr x: string }',
            'decorator tag does not allow this target',
        ),
        ('decorator @unused()', 'decorator requires explicit @targets'),
        ('@tag entity A {} decorator @tag()', 'decorator requires explicit @targets'),
        (
            '@targets(DecoratorTargets.None) decorator @tag() @tag entity A {}',
            'decorator tag does not allow this target',
        ),
        (
            '@targets(DecoratorTargets.All - DecoratorTargets.Entity) decorator @tag() @tag entity A {}',
            'decorator tag does not allow this target',
        ),
        (
            '@targets(DecoratorTargets.EntityAttr, categories=AttrCats.None) decorator @tag() entity A { @tag attr x: string }',
            'decorator tag does not allow this target',
        ),
        (
            '@targets(DecoratorTargets.ValueAttr, categories=AttrCats.Config) decorator @tag() value item { @tag attr x: string }',
            'decorator tag does not allow this target',
        ),
        (
            '@targets(DecoratorTargets.EntityAttr, categories=AttrCats.Config) decorator @tag() entity A { @tag static attr x: string }',
            'decorator tag does not allow this target',
        ),
        (
            '@targets(DecoratorTargets.All) @targets(DecoratorTargets.Entity) decorator @tag()',
            'duplicate decorator: targets',
        ),
        ('@targets() decorator @tag() @tag entity A {}', 'missing decorator argument: targets'),
        (
            '@targets(AttrCats.All) decorator @tag() @tag entity A {}',
            'incompatible decorator argument',
        ),
        (
            '@targets(DecoratorTargets.All, categories=DecoratorTargets.All) decorator @tag() @tag entity A {}',
            'incompatible decorator argument',
        ),
        ("@targets('Entity') decorator @tag() @tag entity A {}", 'incompatible decorator argument'),
        (
            '@targets(DecoratorTargets.Missing) decorator @tag() @tag entity A {}',
            'unknown Decodes code',
        ),
        (
            '@targets(DecoratorTargets.Entity) entity A {}',
            'decorator targets does not allow this target',
        ),
        (
            'entity A { @targets(DecoratorTargets.EntityAttr) attr x: string }',
            'decorator targets does not allow this target',
        ),
        (
            '@targets(DecoratorTargets.All) decorator @tag() @tag decodes Codes { @tag - 1 One: \'One\' }',
            'invalid YAMBL syntax',
        ),
    ]
)


class DecoratorTests(unittest.TestCase):
    def test_descriptor_owner_uses_original_field_site(self):
        from yambl_compiler.semantics.symbols import field_owner, resolve_scopes
        from yambl_compiler.syntax.parser import children

        doc = document('entity A { attr first: string attr second: integer } type label = string')
        unit = resolve_scopes([doc], [])[0]
        fields = children(children(doc.tree, 'entity')[0], 'field')
        syntax = children(children(fields[1], 'field_body')[0], 'type_expr')[0]
        self.assertEqual(field_owner(unit, syntax).key, ('', 'A'))
        alias = children(children(doc.tree, 'type_alias')[0], 'type_expr')[0]
        self.assertIsNone(field_owner(unit, alias))

    def test_valid_calls(self):
        for sources in VALID:
            with self.subTest(sources=sources):
                group = [sources] if isinstance(sources, str) else sources
                diagnostics = check([document(source, str(i)) for i, source in enumerate(group)])
                self.assertEqual([d for d in diagnostics if d.severity == 'error'], [])

    def test_invalid_calls(self):
        from yambl_compiler.syntax.parser import Diagnostic, parse

        for source, expected in INVALID:
            with self.subTest(source=source):
                parsed = parse(source)
                messages = [parsed] if isinstance(parsed, Diagnostic) else check([parsed])
                self.assertTrue(any(expected in d.message for d in messages), messages)

    def test_format_preserves_call_style_and_annotations(self):
        source = "@title(text = 'Tag') decorator @tag (a:string='x',*b?:string) @tag(a = 'x', source : string) type label=string"
        expected = "@title(text='Tag')\ndecorator @tag(a: string = 'x', *b?: string)\n@tag(a='x', source: string)\ntype label = string\n"
        self.assertEqual(format_document(document(source)), expected)
        self.assertEqual(format_document(document(expected)), expected)
