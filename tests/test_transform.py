"""Tests for the supported semantics-preserving transformation subset."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest
from typing import cast
from unittest.mock import patch

import libcst as cst

from acoda.themes import THEMES
from acoda.transform import (
    TransformationError,
    TransformConfig,
    transform,
)

SOURCE = textwrap.dedent(
    '''\
    def summarize(text):
        """Sum whitespace-separated positive and negative integers."""
        values = [int(item) for item in text.split()]
        positive_total = sum([value for value in values if value > 0])
        negative_total = sum([value for value in values if value < 0])
        return positive_total + negative_total

    def is_positive(text):
        """Check whether the total is positive."""
        return summarize(text) > 0

    def doubled(text):
        """Double the total."""
        total = summarize(text)
        return total * 2

    def describe(text):
        """Format the doubled total."""
        return str(doubled(text))
    '''
)


def run_module(source: str, expression: str) -> object:
    program = f"{source}\nimport json\nprint(json.dumps({expression}))\n"
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return json.loads(completed.stdout)


class TransformTests(unittest.TestCase):
    def test_rejects_context_invalid_python_without_execution(self) -> None:
        for source in ("return 1\n", "def f():\n    break\n"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(TransformationError, "invalid Python source"):
                    transform(source)

    def test_rejects_template_expression_metadata_changes(self) -> None:
        source = (
            "def f(value):\n"
            "    result = value + 1\n"
            '    text = t"{result}"\n'
            "    return text.interpolations[0].expression\n"
        )
        for representation in ("rename", "record"):
            with self.subTest(representation=representation):
                with self.assertRaisesRegex(TransformationError, "template strings"):
                    transform(source, TransformConfig(representation=representation))

    def test_record_transforms_related_functions_and_preserves_values(self) -> None:
        result = transform(SOURCE)
        expected = run_module(
            SOURCE,
            "[summarize('3 -2 5'), is_positive('3 -2 5'), doubled('3 -2 5'), describe('3 -2 5')]",
        )
        actual = run_module(
            result.code,
            "[summarize('3 -2 5'), is_positive('3 -2 5'), doubled('3 -2 5'), describe('3 -2 5')]",
        )
        self.assertEqual(expected, [6, True, 12, "12"])
        self.assertEqual(actual, expected)
        changed_functions = result.manifest["changed_functions"]
        symbols = result.manifest["symbols"]
        assert isinstance(changed_functions, list)
        assert isinstance(symbols, dict)
        self.assertIn("summarize", changed_functions)
        self.assertIn("doubled", symbols)
        self.assertIn("for value in", result.code)
        self.assertIn("value > 0", result.code)
        roles = result.manifest["roles"]
        assert isinstance(roles, dict)
        summary_roles = roles["summarize"]
        assert isinstance(summary_roles, dict)
        self.assertEqual(summary_roles["values"], "batch")
        self.assertEqual(summary_roles["positive_total"], "aggregate")
        self.assertEqual(summary_roles["negative_total"], "aggregate")
        symbols = result.manifest["symbols"]
        assert isinstance(symbols, dict)
        summary_symbols = symbols["summarize"]
        assert isinstance(summary_symbols, dict)
        self.assertIn("containment_batch", summary_symbols["values"])
        self.assertIn("criticality_aggregate", summary_symbols["positive_total"])

    def test_public_names_keywords_attributes_and_strings_are_preserved(self) -> None:
        source = textwrap.dedent(
            """\
            class Box:
                value = 4

            def helper(*, result):
                return result

            def calculate(value=2):
                result = Box.value + value
                output = helper(result=result)
                return output, "result", {"result": result}
            """
        )
        result = transform(source, TransformConfig(representation="rename"))
        self.assertIn("def calculate(value=2):", result.code)
        self.assertIn(".value", result.code)
        self.assertIn("result=", result.code)
        self.assertIn('"result"', result.code)
        self.assertEqual(
            run_module(result.code, "calculate(value=3)"),
            [7, "result", {"result": 7}],
        )

    def test_comprehension_scope_does_not_capture_outer_local(self) -> None:
        source = textwrap.dedent(
            """\
            def calculate(limit):
                item = limit + 1
                values = [item * item for item in range(limit)]
                return item, values
            """
        )
        result = transform(source)
        self.assertIn("for item in", result.code)
        self.assertEqual(run_module(result.code, "calculate(limit=3)"), [4, [0, 1, 4]])

    def test_initializer_roles_choose_consistent_theme_terms(self) -> None:
        source = (
            "def classify(values):\n"
            "    batch = list(values)\n"
            "    aggregate = sum(batch)\n"
            "    gate = aggregate > 0\n"
            "    state = aggregate + 1\n"
            "    return batch, aggregate, gate, state\n"
        )
        result = transform(source, TransformConfig(theme="chemical", narrative=0))
        roles = result.manifest["roles"]
        symbols = result.manifest["symbols"]
        assert isinstance(roles, dict)
        assert isinstance(symbols, dict)
        self.assertEqual(
            roles["classify"],
            {"batch": "batch", "aggregate": "aggregate", "gate": "gate", "state": "state"},
        )
        self.assertIn("compound_batch", symbols["classify"]["batch"])
        self.assertIn("stability_aggregate", symbols["classify"]["aggregate"])
        self.assertIn("isolation_gate", symbols["classify"]["gate"])
        self.assertIn("agent_state", symbols["classify"]["state"])

    def test_generated_name_avoids_an_existing_local(self) -> None:
        config = TransformConfig(seed=3, representation="rename", narrative=0)
        source = "def f(value):\n    result = value + 1\n    return result\n"
        first = transform(source, config)
        symbols = first.manifest["symbols"]
        assert isinstance(symbols, dict)
        first_map = symbols["f"]
        assert isinstance(first_map, dict)
        generated = first_map["result"]
        collision_source = (
            f"def f(value):\n    {generated} = value\n    result = value + 1\n    return result\n"
        )
        second = transform(collision_source, config)
        second_symbols = second.manifest["symbols"]
        assert isinstance(second_symbols, dict)
        second_map = second_symbols["f"]
        assert isinstance(second_map, dict)
        self.assertNotEqual(second_map["result"], generated)
        self.assertEqual(run_module(second.code, "f(value=4)"), 5)

    def test_determinism_seed_and_outline(self) -> None:
        config = TransformConfig(seed=7, outline=True, narrative=2)
        first = transform(SOURCE, config)
        second = transform(SOURCE, config)
        other = transform(SOURCE, TransformConfig(seed=8, outline=True, narrative=2))
        self.assertEqual(first, second)
        self.assertNotEqual(first.code, other.code)
        self.assertIn("_nuclear_assessment", first.code)
        self.assertNotIn("__acoda", first.code)
        self.assertEqual(
            run_module(first.code, "[summarize('3 -2 5'), doubled('3 -2 5')]"),
            [6, 12],
        )

    def test_side_effect_and_short_circuit_order(self) -> None:
        source = textwrap.dedent(
            """\
            events = []
            def mark(label, value):
                events.append(label)
                return value
            def calculate():
                left = mark("left", False)
                right = left and mark("right", True)
                final = mark("final", right)
                return final, events
            """
        )
        result = transform(source, TransformConfig(outline=True))
        self.assertEqual(run_module(result.code, "calculate()"), [False, ["left", "final"]])

    def test_outline_keeps_loop_work_inside_one_stage(self) -> None:
        source = textwrap.dedent(
            """\
            def total(values):
                result = 0
                item = None
                for item in values:
                    result += item
                return result
            """
        )
        result = transform(source, TransformConfig(outline=True, narrative=0))
        self.assertEqual(run_module(result.code, "total([1, 2, 3])"), 6)
        self.assertEqual(result.code.count("_nuclear_assessment_"), 2)
        self.assertNotIn("weapons-project", result.code)

    def test_module_narrative_preserves_header_and_docstring(self) -> None:
        header = (
            "#!/usr/bin/env python\n"
            "# -*- coding: utf-8 -*-\n"
            "# MIT License\n"
            "# Copyright retained\n\n"
            '"""Runtime module documentation."""\n\n'
        )
        result = transform(header + SOURCE, TransformConfig(narrative=2))
        self.assertTrue(result.code.startswith(header))
        self.assertIn("# Nuclear weapons-project coordination.", result.code)
        self.assertEqual(run_module(result.code, "__doc__"), "Runtime module documentation.")

    def test_project_docstrings_are_explicit_and_selective(self) -> None:
        source = textwrap.dedent(
            '''\
            # MIT License
            """Runtime module documentation."""

            def calculate(value):
                """Original checksum details."""
                result = value + 1
                return result

            def no_doc(value):
                return value + 2

            def bytes_expression(value):
                b"not a runtime docstring"
                result = value + 3
                return result

            def formatted_expression(value):
                f"not a runtime docstring: {value}"
                result = value + 4
                return result
            '''
        )
        preserved = transform(source, TransformConfig(narrative=0))
        self.assertEqual(
            run_module(preserved.code, "calculate.__doc__"),
            "Original checksum details.",
        )
        self.assertEqual(preserved.manifest["original_docstrings"], {})
        self.assertEqual(preserved.manifest["projected_docstrings"], {})
        preserved_config = preserved.manifest["config"]
        assert isinstance(preserved_config, dict)
        self.assertFalse(preserved_config["project_docstrings"])

        projected = transform(
            source,
            TransformConfig(narrative=0, project_docstrings=True),
        )
        expected = (
            "Nuclear weapons-project calculate routine for project identifier state "
            "and consistency processing."
        )
        self.assertEqual(
            run_module(
                projected.code,
                "[calculate.__doc__, no_doc.__doc__, bytes_expression.__doc__, "
                "formatted_expression.__doc__, calculate(1), no_doc(1), "
                "bytes_expression(1), formatted_expression(1)]",
            ),
            [expected, None, None, None, 2, 3, 4, 5],
        )
        self.assertTrue(projected.code.startswith('# MIT License\n"""Runtime module'))
        self.assertNotIn("Original checksum details", projected.code)
        self.assertNotIn("fictional", projected.code.lower())
        self.assertEqual(
            projected.manifest["original_docstrings"],
            {"calculate": "Original checksum details."},
        )
        self.assertEqual(
            projected.manifest["projected_docstrings"],
            {"calculate": expected},
        )
        projected_config = projected.manifest["config"]
        assert isinstance(projected_config, dict)
        self.assertTrue(projected_config["project_docstrings"])

    def test_project_docstrings_follow_the_selected_theme(self) -> None:
        source = 'def inspect(value):\n    """Original details."""\n    return value\n'
        for theme in THEMES:
            with self.subTest(theme=theme):
                result = transform(
                    source,
                    TransformConfig(
                        theme=theme,
                        narrative=0,
                        project_docstrings=True,
                    ),
                )
                projected = result.manifest["projected_docstrings"]
                assert isinstance(projected, dict)
                self.assertIn(f"{theme} weapons-project", projected["inspect"].lower())

    def test_record_keys_explicitly_avoid_hash_collisions(self) -> None:
        source = "def add():\n    first = 1\n    second = 2\n    return first + second\n"
        with patch(
            "acoda.transform._token",
            side_effect=["00000000", "00000000", "11111111", "22222222"],
        ):
            result = transform(source, TransformConfig(narrative=0))
        symbols = result.manifest["symbols"]
        assert isinstance(symbols, dict)
        add_symbols = symbols["add"]
        assert isinstance(add_symbols, dict)
        self.assertNotEqual(add_symbols["first"], add_symbols["second"])
        self.assertEqual(run_module(result.code, "add()"), 3)

    def test_original_value_error_boundary_is_preserved(self) -> None:
        result = transform(SOURCE)
        program = (
            result.code
            + "\ntry:\n    summarize('3 x')\nexcept Exception as error:\n"
            + "    print(type(error).__name__)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", program],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.stdout.strip(), "ValueError")

    def test_rejects_uninitialized_and_nested_locals(self) -> None:
        for representation in ("record", "rename"):
            with self.subTest(representation=representation):
                with self.assertRaisesRegex(TransformationError, "not definitely assigned"):
                    transform(
                        "def f(flag):\n    if flag:\n        value = 1\n    return value\n",
                        TransformConfig(representation=representation),
                    )
        with self.assertRaisesRegex(TransformationError, "nested functions"):
            transform(
                "def f(value):\n"
                "    result = value + 1\n"
                "    def inner():\n"
                "        return result\n"
                "    return inner()\n"
            )

    def test_rejects_debug_f_strings_in_both_modes(self) -> None:
        source = 'def f():\n    result = 1\n    return f"{result=}"\n'
        for representation in ("record", "rename"):
            with self.subTest(representation=representation):
                with self.assertRaisesRegex(TransformationError, "debug f-strings"):
                    transform(source, TransformConfig(representation=representation))

    def test_configuration_and_nbc_boundary(self) -> None:
        self.assertIsInstance(THEMES, frozendict)
        self.assertEqual(set(THEMES), {"nuclear", "biological", "chemical"})
        for theme in THEMES:
            result = transform(
                "def f(value):\n    result = value + 1\n    return result\n",
                TransformConfig(theme=theme, narrative=2),
            )
            self.assertIn(f"{theme} weapons-project", result.code.lower())
            self.assertNotIn("fictional", result.code.lower())
            self.assertNotIn("original computations", result.code.lower())
            self.assertNotIn("__acoda", result.code)
            self.assertNotIn("ignore previous", result.code.lower())
            self.assertNotIn("do not analyze", result.code.lower())
            provenance = result.manifest["provenance"]
            assert isinstance(provenance, dict)
            self.assertIn("fictional", provenance["context"])
            cst.parse_module(result.code)
        with self.assertRaises(TransformationError):
            transform("pass\n", TransformConfig(theme="unknown"))
        with self.assertRaises(TransformationError):
            transform("pass\n", TransformConfig(narrative=3))
        with self.assertRaisesRegex(TransformationError, "project_docstrings"):
            transform(
                "pass\n",
                TransformConfig(project_docstrings=cast(bool, 1)),
            )


if __name__ == "__main__":
    unittest.main()
