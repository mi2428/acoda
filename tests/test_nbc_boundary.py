"""NBC appearance must not introduce a new executable capability."""

import ast
import re
import unittest
from collections import Counter

from acoda.themes import THEMES
from acoda.transform import TransformConfig, transform


def operators(tree: ast.AST) -> Counter[str]:
    return Counter(
        type(node).__name__
        for node in ast.walk(tree)
        if isinstance(node, (ast.operator, ast.unaryop, ast.boolop, ast.cmpop))
    )


def numeric_literals(tree: ast.AST) -> Counter[str]:
    return Counter(
        repr(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, complex))
    )


class NBCBoundaryTests(unittest.TestCase):
    def test_all_profiles_keep_math_and_only_add_private_stage_calls(self) -> None:
        source = (
            "def calculate(values):\n"
            '    """Sum the squares of positive values."""\n'
            "    squares = [value * value for value in values if value > 0]\n"
            "    total = sum(squares)\n"
            "    return total\n"
        )
        original = ast.parse(source)
        original_calls = {
            node.func.id
            for node in ast.walk(original)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        banned = re.compile(
            r"ignore (?:all|previous)|do not (?:analy[sz]e|deobfuscate)|"
            r"no code to return|<\|[a-z_]+\|>|ransomware|trojan",
            re.IGNORECASE,
        )
        for theme in THEMES:
            for representation in ("rename", "record"):
                for outline in (False, True):
                    for projected in (False, True):
                        with self.subTest(
                            theme=theme,
                            representation=representation,
                            outline=outline,
                            projected=projected,
                        ):
                            result = transform(
                                source,
                                TransformConfig(
                                    theme=theme,
                                    representation=representation,
                                    outline=outline,
                                    project_docstrings=projected,
                                    narrative=2,
                                ),
                            )
                            tree = ast.parse(result.code)
                            outlines = result.manifest["outlines"]
                            assert isinstance(outlines, dict)
                            helpers = {name for names in outlines.values() for name in names}
                            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
                            self.assertTrue(all(isinstance(node.func, ast.Name) for node in calls))
                            self.assertLessEqual(
                                {node.func.id for node in calls if isinstance(node.func, ast.Name)},
                                original_calls | helpers,
                            )
                            self.assertEqual(operators(tree), operators(original))
                            self.assertEqual(numeric_literals(tree), numeric_literals(original))
                            self.assertFalse(
                                any(
                                    isinstance(node, (ast.Import, ast.ImportFrom))
                                    for node in ast.walk(tree)
                                )
                            )
                            self.assertIsNone(banned.search(result.code))


if __name__ == "__main__":
    unittest.main()
