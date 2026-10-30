"""Check multi-hop reverse relationship contracts and diagnostic locations."""

import unittest

from tests.support import document
from yambl_compiler.languages.yambl.semantics.validation import analyze


class ReversePathSemanticsTests(unittest.TestCase):
    def test_multihop_requires_forward_single_relationships(self):
        source = '''entity A { id id: string @relationship(one, maybe) attr b: B }
entity B { id id: string @relationship(one, maybe) attr c: C }
entity C { id id: string @reverse_relationship(A.b.c) attr a: A }'''
        self.assertEqual(
            [d for d in analyze([document(source)]).diagnostics if d.severity == 'error'], []
        )
        for edited, expected in (
            (
                source.replace('@relationship(one, maybe) attr c: C', 'attr c: C'),
                'hop 2 c must be a forward',
            ),
            (
                source.replace(
                    '@relationship(one, maybe) attr c: C', '@relationship(many, maybe) attr c: C'
                ),
                'hop 2 c requires a one-to-one',
            ),
            (
                source.replace(
                    '@relationship(one, maybe) attr c: C',
                    '@relationship(one, many) attr c: collection<C>',
                ),
                'hop 2 c requires a one-to-one',
            ),
            (source.replace('A.b.c)', 'A.b.missing)'), 'hop 2 missing is not a visible field'),
            (
                source.replace('attr c: C', 'scd attr c: C'),
                'categories and temporal parameters must match at hop 2',
            ),
        ):
            with self.subTest(edited=edited):
                messages = [d.message for d in analyze([document(edited)]).diagnostics]
                self.assertTrue(any(expected in message for message in messages), messages)

    def test_each_hop_requires_the_same_dimension_identity(self):
        source = '''dimension shared { value source: string }
dimension other { value source: string }
entity A { id id: string @dimension(shared) @relationship(one, maybe) attr b: B }
entity B { id id: string @dimension(other) @relationship(one, maybe) attr c: C }
entity C { id id: string @dimension(shared) @reverse_relationship(A.b.c) attr a: A }'''
        messages = [d.message for d in analyze([document(source)]).diagnostics]
        self.assertTrue(
            any(
                'dimensions must match by declaration identity at hop 2' in message
                for message in messages
            ),
            messages,
        )
