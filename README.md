# Acoda

A deterministic Python 3.15 source-to-source transformer that applies fictional
nuclear, biological, and chemical (NBC) terminology to harmless programs.
It renames internal bindings and can reorganize computation into themed state
and stages. It does not execute input source, call a model, or require a service.

Inspired by Rao et al., *Acoda: Adversarial Code Obfuscation for Defending against
LLM-based Analysis*, [arXiv:2606.11755](https://arxiv.org/abs/2606.11755), 10 June 2026.
This is an independent, transformation-only implementation, not a reproduction
of the full research system.

## Transform a source file

Use a current [uv](https://docs.astral.sh/uv/). The checkout pins CPython 3.15.2 and
LibCST 1.9.0; development tools are Ruff and ty.

```sh
uv sync
uv run acoda input.py \
  --theme nuclear --seed 17 --representation record --outline --narrative 2 \
  --output build/converted.py
```

Save a UTF-8 Python module as `input.py`, then choose a fresh output path.
`build/converted.py.json` records configuration, mappings, documentation changes,
and unapplied features. Existing source/output files are never overwritten;
handled write failures remove only files created by that invocation. This is not
a crash-atomic two-file transaction. Keep original licenses with derivatives.

Themes are `nuclear`, `biological`, and `chemical`. Fixed source, configuration,
seed, and implementation produce deterministic output.

- `--representation rename`: rename internal bindings; retain public parameters.
- `--representation record`: store locals in private, themed per-call state.
- `--outline`: split compatible record-based functions into two coarse stages.
- `--narrative 0/1/2`: no descriptions, short context, or explicit project context.
- `--project-docstrings`: replace existing function docs, intentionally changing
  `__doc__`. Default mode preserves them; the manifest records old and new text.

Supported inputs are module-level synchronous functions with indented bodies and
directly initialized locals. Class methods and async functions remain unchanged
and are reported as unapplied; unsupported function constructs raise
`TransformationError`. Nested functions/classes, lambdas, local imports, template
strings, generators, and complex control constructs are outside the subset.

Within that subset, preservation requirements cover public signatures, ordinary
results, external values, evaluation order, exceptions, and license notices.
Tests are not an equivalence proof for arbitrary Python. Frame/source/local-name
inspection and lifetime observers are unsupported; known finalizers and weak
references are rejected. Outlining changes stack structure and tracebacks, and
is skipped for direct module-call cycles. Indirect recursion through aliases,
callbacks, or imports is unresolved; stack-limit-sensitive programs using those
paths are outside the outlining contract.

## Python API

```python
from acoda.transform import TransformConfig, transform

source = "def total(values):\n    amount = sum(values)\n    return amount\n"
result = transform(source, TransformConfig(theme="biological", seed=17, outline=True))
print(result.code)
print(result.manifest)
```

Invalid configuration, invalid Python, and unsupported constructs raise
`TransformationError`. Optional transformations that cannot apply are described
in the manifest rather than silently reported as successful.

