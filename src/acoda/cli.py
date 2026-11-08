"""Apply fictional NBC-themed transformations to a Python source file."""

import argparse
import json
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Literal, cast

lazy from .transform import TransformationError, TransformConfig, TransformResult, transform


def parser() -> argparse.ArgumentParser:
    """Build the converter CLI without loading LibCST for help output."""
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("source", type=Path, help="UTF-8 Python source file")
    root.add_argument("--output", type=Path, required=True)
    root.add_argument("--theme", choices=("nuclear", "biological", "chemical"), default="nuclear")
    root.add_argument("--seed", type=int, default=0)
    root.add_argument("--representation", choices=("rename", "record"), default="record")
    root.add_argument("--outline", action="store_true")
    root.add_argument("--narrative", type=int, choices=(0, 1, 2), default=1)
    root.add_argument(
        "--project-docstrings",
        action="store_true",
        help="replace function documentation with NBC context; intentionally changes __doc__",
    )
    return root


def _write_transformation(output: Path, result: TransformResult) -> Path:
    """Create both outputs exclusively; roll back owned files on ordinary failures.

    This is not a crash-atomic, two-file transaction. Existing paths are never
    registered for cleanup, even if they appear after the CLI's initial check.
    """
    manifest = output.with_suffix(output.suffix + ".json")
    metadata = json.dumps(result.manifest, indent=2, ensure_ascii=False) + "\n"
    with ExitStack() as rollback:
        with output.open("x", encoding="utf-8") as code_stream:
            rollback.callback(output.unlink)
            with manifest.open("x", encoding="utf-8") as metadata_stream:
                rollback.callback(manifest.unlink)
                code_stream.write(result.code)
                metadata_stream.write(metadata)
        rollback.pop_all()
    return manifest


def main(argv: Sequence[str] | None = None) -> None:
    """Transform one source file and report expected input failures without a traceback."""
    root = parser()
    args = root.parse_args(argv)
    config = TransformConfig(
        theme=args.theme,
        seed=args.seed,
        representation=cast(Literal["rename", "record"], args.representation),
        outline=args.outline,
        narrative=args.narrative,
        project_docstrings=args.project_docstrings,
    )
    output: Path = args.output
    manifest = output.with_suffix(output.suffix + ".json")
    if args.source.resolve() == output.resolve():
        root.error("the source and output paths must differ")
    if output.exists() or manifest.exists():
        root.error("output or manifest already exists; choose a new destination")
    try:
        result = transform(args.source.read_text(encoding="utf-8"), config)
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_transformation(output, result)
    except (OSError, UnicodeError, TransformationError) as error:
        root.exit(2, f"error: {error}\n")
    print(json.dumps({"output": str(output), "manifest": str(manifest)}))
