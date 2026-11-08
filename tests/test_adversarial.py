"""Adversarial behavior checks for the supported transformation subset."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest

from acoda.transform import TransformationError, TransformConfig, transform

CONFIGS = tuple(
    TransformConfig(representation=representation, outline=outline, narrative=0)
    for representation in ("rename", "record")
    for outline in (False, True)
)


def observe(source: str, expression: str) -> object:
    program = source + textwrap.dedent(
        f"""\

        import json
        try:
            _value = {expression}
        except Exception as _error:
            _observation = {{
                "kind": "raise",
                "type": type(_error).__name__,
                "args": repr(_error.args),
            }}
        else:
            _observation = {{"kind": "return", "value": _value}}
        _observation["events"] = globals().get("events")
        print(json.dumps(_observation, sort_keys=True))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return json.loads(completed.stdout)


class AdversarialBehaviorTests(unittest.TestCase):
    def test_direct_and_mutual_recursion_keep_their_stack_depth(self) -> None:
        sources = (
            "def descend(n):\n    result = n\n    if n:\n"
            "        result = descend(n - 1)\n    return result\n",
            "def descend(n):\n    result = n\n    if n:\n"
            "        result = other(n - 1)\n    return result\n"
            "def other(n):\n    result = n\n    if n:\n"
            "        result = descend(n - 1)\n    return result\n",
        )
        for source in sources:
            result = transform(source, TransformConfig(outline=True, narrative=0))
            with self.subTest(source=source):
                self.assertEqual(result.manifest["outlines"], {})
                self.assertIn("recursive module calls", str(result.manifest["unapplied"]))
                for code in (source, result.code):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-c",
                            "import sys\nsys.setrecursionlimit(100)\n"
                            + code
                            + "\nassert descend(50) == 0\n",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)

    def assert_equivalent(self, source: str, expression: str) -> None:
        source = textwrap.dedent(source)
        expected = observe(source, expression)
        for config in CONFIGS:
            with self.subTest(
                representation=config.representation,
                outline=config.outline,
            ):
                actual = observe(transform(source, config).code, expression)
                self.assertEqual(actual, expected)

    def test_aliases_chained_assignment_and_external_data(self) -> None:
        self.assert_equivalent(
            """
            class Box:
                def __init__(self, values):
                    self.values = values

            def probe():
                original = Box([1])
                alias = original
                first = second = [2]
                first.append(3)
                alias.values.extend(first)
                external = {"value": original.values[-1]}
                result = external["value"] + second[0]
                return result, alias is original, external, original.values
            """,
            "probe()",
        )

    def test_starred_unpacking_preserves_iterator_effect_order(self) -> None:
        self.assert_equivalent(
            """
            events = []
            class Source:
                def __iter__(self):
                    events.append("iter")
                    for value in (1, 2, 3, 4):
                        events.append(["yield", value])
                        yield value

            def probe():
                first = 0
                middle = []
                last = 0
                first, *middle, last = Source()
                return first, middle, last
            """,
            "probe()",
        )

    def test_comprehensions_keep_outer_and_shadowed_scopes_distinct(self) -> None:
        self.assert_equivalent(
            """
            def probe(limit):
                offset = limit + 1
                values = [offset + item for item in range(limit)]
                shadow = [offset * 2 for offset in range(3)]
                nested = [offset + left + right for left in range(2) for right in range(2)]
                mapping = {item: offset + item for item in range(2)}
                generated = sum(offset + item for item in range(2))
                return offset, values, shadow, nested, mapping, generated
            """,
            "probe(3)",
        )

    def test_short_circuit_and_regular_formatting_call_protocols(self) -> None:
        self.assert_equivalent(
            """
            events = []
            class Signal:
                def __init__(self, enabled):
                    self.enabled = enabled
                def __bool__(self):
                    events.append(["bool", self.enabled])
                    return self.enabled
                def __format__(self, spec):
                    events.append(["format", spec])
                    return f"signal:{spec}"

            def mark():
                events.append("mark")
                return 7

            def probe(enabled):
                signal = Signal(enabled)
                selected = signal and mark()
                retained = selected is signal
                rendered = f"{signal:wide}"
                value = None if retained else selected
                return retained, value, rendered
            """,
            "[probe(False), probe(True)]",
        )

    def test_augmented_operator_keeps_alias_and_effect_order(self) -> None:
        self.assert_equivalent(
            """
            events = []
            class Number:
                def __init__(self, value):
                    self.value = value
                def __iadd__(self, other):
                    events.append(["iadd", other])
                    self.value += other
                    return self

            def probe():
                number = Number(1)
                alias = number
                increment = 2
                number += increment
                result = number.value
                return result, alias.value, alias is number
            """,
            "probe()",
        )

    def test_empty_loop_for_else_and_break_paths(self) -> None:
        self.assert_equivalent(
            """
            def probe(values):
                total = 0
                item = None
                for item in values:
                    if item < 0:
                        break
                    total += item
                else:
                    total += 100
                return total, item
            """,
            "[probe([]), probe([1, 2]), probe([1, -1, 5])]",
        )

    def test_parameter_reassignment_keeps_outline_unapplied(self) -> None:
        source = textwrap.dedent(
            """
            def probe(value):
                value += 1
                result = value * 2
                final = result + 1
                return value, final
            """
        )
        self.assert_equivalent(source, "probe(4)")
        transformed = transform(
            source,
            TransformConfig(representation="record", outline=True, narrative=0),
        )
        unapplied = transformed.manifest["unapplied"]
        assert isinstance(unapplied, list)
        self.assertIn(
            "parameter reassignment cannot cross stage scopes",
            {entry["reason"] for entry in unapplied},
        )

    def test_exception_and_prior_effect_order_match(self) -> None:
        self.assert_equivalent(
            """
            events = []
            def mark(label, value):
                events.append(label)
                return value

            def probe():
                numerator = mark("numerator", 4)
                denominator = mark("denominator", 0)
                result = numerator // denominator
                return result
            """,
            "probe()",
        )

    def test_explicit_lifetime_observation_is_rejected(self) -> None:
        sources = {
            "finalizer": """
                events = []
                class Token:
                    def __init__(self, name):
                        self.name = name
                    def __del__(self):
                        events.append(self.name)
                def probe():
                    first = Token("first")
                    second = Token("second")
                    first = Token("third")
                    return "done"
            """,
            "weakref": """
                import weakref
                def probe(value):
                    reference = weakref.ref(value)
                    return reference
            """,
        }
        for name, source in sources.items():
            for representation in ("rename", "record"):
                with self.subTest(name=name, representation=representation):
                    with self.assertRaisesRegex(TransformationError, "object lifetime observation"):
                        transform(
                            textwrap.dedent(source),
                            TransformConfig(representation=representation),
                        )


if __name__ == "__main__":
    unittest.main()
