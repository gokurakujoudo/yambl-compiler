"""Named dimension declaration, inheritance and application contracts."""

import unittest

from tests.support import document
from yambl_compiler.semantics.validation import check


class DimensionTests(unittest.TestCase):
    def errors(self, source):
        return [d.message for d in check([document(source)]) if d.severity == "error"]

    def test_forward_extension_and_inheritance(self):
        source = """
            entity E { @dimension(prices) static attr price: decimal }
            extend E { dimension prices { value source: string hierarchy hierarchy: Hierarchy } }
            entity Child inherits E { @dimension(prices) static attr other: decimal }
        """
        self.assertEqual(self.errors(source), [])

    def test_declaration_and_application_errors(self):
        cases = [
            (
                "entity E { @dimension(source: string) static attr price: decimal }",
                "inline @dimension",
            ),
            (
                "entity E { @dimension(missing) static attr price: decimal }",
                "unknown dimension",
            ),
            (
                "entity E { dimension prices { value source: integer } }",
                "string or code",
            ),
            (
                "entity E { dimension prices { value source: string hierarchy h: string } }",
                "requires Hierarchy",
            ),
            (
                "entity E { dimension prices { value source: string hierarchy h: Hierarchy = 1 } }",
                "string reference ID",
            ),
            ("entity E { dimension Prices { value source: string } }", "snake_case"),
            (
                "entity E { dimension p { value s: string } dimension p { value t: string } }",
                "duplicate dimension declaration",
            ),
            (
                "entity E { static attr p: string dimension p { value s: string } }",
                "conflicting dimension",
            ),
            (
                "entity E { dimension p { value s: string } } entity F inherits E { dimension p { value t: string } }",
                "conflicting dimension",
            ),
            (
                "entity E { dimension p { value day: string } @temporal(day: date) @dimension(p) snapshot attr x: decimal }",
                "duplicate dimension",
            ),
            (
                "entity E { dimension p { value s: string fallback_days = 2 } @dimension(p) static attr x: decimal }",
                "only on snapshot",
            ),
            ("entity E { static attr d: dimension_ref }", "only a metadata"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                errors = self.errors(source)
                self.assertTrue(any(expected in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
