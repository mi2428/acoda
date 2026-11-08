"""Deterministic, semantics-conscious fictional NBC source transformations."""

from __future__ import annotations

import hashlib
import keyword
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import libcst as cst
from libcst.metadata import (
    Assignment,
    CodePosition,
    CodeRange,
    FunctionScope,
    MetadataWrapper,
    PositionProvider,
    ScopeProvider,
)
from libcst.metadata.scope_provider import Scope

from acoda.themes import THEMES, SymbolRole, Theme


@dataclass(frozen=True, slots=True)
class TransformConfig:
    """Deterministic appearance options; docstring projection is opt-in."""

    theme: str = "nuclear"
    seed: int = 0
    representation: Literal["rename", "record"] = "record"
    outline: bool = False
    narrative: int = 1
    project_docstrings: bool = False


@dataclass(frozen=True, slots=True)
class TransformResult:
    """Generated source and the manifest describing applied and skipped changes."""

    code: str
    manifest: dict[str, object]


class TransformationError(ValueError):
    """Raised for invalid input or an unsupported or unsafe transformation."""


class _AllNames(cst.CSTVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)


def _root_name(node: cst.CSTNode | None) -> str | None:
    while isinstance(node, cst.Attribute):
        node = node.value
    return node.value if isinstance(node, cst.Name) else None


class _LifetimeObservation(cst.CSTVisitor):
    """Find explicit object-lifetime observation outside the supported contract."""

    def __init__(self) -> None:
        self.reasons: set[str] = set()
        self._class_depth = 0

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._class_depth += 1

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self._class_depth -= 1

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        if self._class_depth and node.name.value == "__del__":
            self.reasons.add("class finalizers")

    def visit_Assign(self, node: cst.Assign) -> None:
        if self._class_depth and any(
            target.value == "__del__"
            for assign_target in node.targets
            for target in _target_names(assign_target.target)
        ):
            self.reasons.add("class finalizers")

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        if (
            self._class_depth
            and isinstance(node.target, cst.Name)
            and node.target.value == "__del__"
        ):
            self.reasons.add("class finalizers")

    def visit_Import(self, node: cst.Import) -> None:
        if any(_root_name(alias.name) == "weakref" for alias in node.names):
            self.reasons.add("weakref usage")

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if _root_name(node.module) == "weakref":
            self.reasons.add("weakref usage")

    def visit_Attribute(self, node: cst.Attribute) -> None:
        if _root_name(node) == "weakref":
            self.reasons.add("weakref usage")


class _UnsupportedSyntax(cst.CSTVisitor):
    """Find constructs outside the deliberately small transformation subset."""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    def _reject(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self._reject("nested functions")
        return False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self._reject("nested classes")
        return False

    def visit_Lambda(self, node: cst.Lambda) -> bool:
        self._reject("lambda scopes")
        return False

    def visit_Global(self, node: cst.Global) -> None:
        self._reject("global declarations")

    def visit_Nonlocal(self, node: cst.Nonlocal) -> None:
        self._reject("nonlocal declarations")

    def visit_Import(self, node: cst.Import) -> None:
        self._reject("function-local imports")

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        self._reject("function-local imports")

    def visit_Try(self, node: cst.Try) -> None:
        self._reject("try statements")

    def visit_TryStar(self, node: cst.TryStar) -> None:
        self._reject("try-star statements")

    def visit_With(self, node: cst.With) -> None:
        self._reject("with statements")

    def visit_Match(self, node: cst.Match) -> None:
        self._reject("match statements")

    def visit_Del(self, node: cst.Del) -> None:
        self._reject("del statements")

    def visit_NamedExpr(self, node: cst.NamedExpr) -> None:
        self._reject("assignment expressions")

    def visit_TypeAlias(self, node: cst.TypeAlias) -> None:
        self._reject("local type aliases")

    def visit_Yield(self, node: cst.Yield) -> None:
        self._reject("generator functions")

    def visit_Await(self, node: cst.Await) -> None:
        self._reject("await expressions")

    def visit_FormattedStringExpression(self, node: cst.FormattedStringExpression) -> None:
        if node.equal is not None:
            self._reject("debug f-strings")

    def visit_TemplatedString(self, node: cst.TemplatedString) -> None:
        self._reject("template strings with observable expression text")

    def visit_For(self, node: cst.For) -> None:
        if node.asynchronous is not None:
            self._reject("async for statements")

    def visit_Call(self, node: cst.Call) -> None:
        if isinstance(node.func, cst.Name) and node.func.value in {
            "eval",
            "exec",
            "locals",
            "vars",
        }:
            self._reject(f"local namespace introspection via {node.func.value}()")
        if isinstance(node.func, cst.Name) and node.func.value == "dir" and not node.args:
            self._reject("local namespace introspection via dir()")


class _AssignmentTargets(cst.CSTVisitor):
    def __init__(self) -> None:
        self.targets: list[cst.Name] = []

    def _add(self, target: cst.CSTNode) -> None:
        self.targets.extend(_target_names(target))

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        return False

    def visit_Lambda(self, node: cst.Lambda) -> bool:
        return False

    def visit_Assign(self, node: cst.Assign) -> None:
        for target in node.targets:
            self._add(target.target)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        self._add(node.target)

    def visit_AugAssign(self, node: cst.AugAssign) -> None:
        self._add(node.target)

    def visit_For(self, node: cst.For) -> None:
        self._add(node.target)


class _NodeSet(cst.CSTVisitor):
    def __init__(self) -> None:
        self.ids: set[int] = set()

    def on_visit(self, node: cst.CSTNode) -> bool:
        self.ids.add(id(node))
        return True


class _NameMapper(cst.CSTTransformer):
    def __init__(
        self,
        scopes: Mapping[cst.CSTNode, Scope | None],
        function_scope: FunctionScope,
        replacements: dict[str, str],
        *,
        state_name: str | None,
    ) -> None:
        self._scopes = scopes
        self._function_scope = function_scope
        self._replacements = replacements
        self._state_name = state_name

    def _is_function_local(self, node: cst.Name) -> bool:
        if node.value not in self._replacements:
            return False
        scope = self._scopes.get(node)
        if scope is None:
            return False
        return any(assignment.scope is self._function_scope for assignment in scope[node.value])

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.BaseExpression:
        if not self._is_function_local(original_node):
            return updated_node
        replacement = self._replacements[original_node.value]
        if self._state_name is None:
            return cst.Name(replacement)
        return cst.parse_expression(f"{self._state_name}[{replacement!r}]")

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.Attribute:
        return updated_node.with_changes(attr=original_node.attr)

    def leave_Arg(self, original_node: cst.Arg, updated_node: cst.Arg) -> cst.Arg:
        return updated_node.with_changes(keyword=original_node.keyword)

    def leave_AnnAssign(
        self, original_node: cst.AnnAssign, updated_node: cst.AnnAssign
    ) -> cst.AnnAssign:
        return updated_node.with_changes(annotation=original_node.annotation)


def _target_names(target: cst.CSTNode) -> list[cst.Name]:
    if isinstance(target, cst.Name):
        return [target]
    if isinstance(target, cst.StarredElement):
        return _target_names(target.value)
    if isinstance(target, (cst.List, cst.Tuple)):
        names: list[cst.Name] = []
        for element in target.elements:
            if element is not None:
                names.extend(_target_names(element.value))
        return names
    return []


def _parameter_names(params: cst.Parameters) -> set[str]:
    parameters = [*params.posonly_params, *params.params, *params.kwonly_params]
    if isinstance(params.star_arg, cst.Param):
        parameters.append(params.star_arg)
    if params.star_kwarg is not None:
        parameters.append(params.star_kwarg)
    return {param.name.value for param in parameters}


def _docstring_value(statement: cst.BaseStatement) -> str | None:
    if not isinstance(statement, cst.SimpleStatementLine) or len(statement.body) != 1:
        return None
    small = statement.body[0]
    if not isinstance(small, cst.Expr) or not isinstance(
        small.value, (cst.SimpleString, cst.ConcatenatedString)
    ):
        return None
    value = small.value.evaluated_value
    return value if isinstance(value, str) else None


def _is_docstring(statement: cst.BaseStatement) -> bool:
    return _docstring_value(statement) is not None


def _project_docstring(theme: Theme, function_name: str) -> str:
    return (
        f"{theme.project_label.capitalize()} {function_name} routine for project "
        "identifier state and consistency processing."
    )


def _replace_docstring(statement: cst.BaseStatement, value: str) -> cst.BaseStatement:
    assert isinstance(statement, cst.SimpleStatementLine)
    expression = statement.body[0]
    assert isinstance(expression, cst.Expr)
    return statement.with_changes(
        body=(expression.with_changes(value=cst.SimpleString(repr(value))),)
    )


def _token(config: TransformConfig, identity: str, purpose: str) -> str:
    material = f"{config.seed}\0{config.theme}\0{identity}\0{purpose}".encode()
    return hashlib.blake2s(material, digest_size=4).hexdigest()


def _fresh_name(
    stem: str,
    config: TransformConfig,
    identity: str,
    purpose: str,
    used_names: set[str],
) -> str:
    counter = 0
    while True:
        token = _token(config, identity, f"{purpose}:{counter}")
        candidate = f"{stem}_{token}"
        if candidate not in used_names and not keyword.iskeyword(candidate):
            used_names.add(candidate)
            return candidate
        counter += 1


def _narrative(theme: Theme, function_name: str, level: int) -> str:
    if level == 1:
        return f"# {theme.namespace.capitalize()} project state."
    return f"# {theme.project_label.capitalize()} state for {function_name}."


def _module_narrative(theme: Theme, level: int) -> str:
    if level == 1:
        return f"# {theme.namespace.capitalize()} project coordination."
    return f"# {theme.project_label.capitalize()} coordination."


def _with_comment(statement: cst.BaseStatement, comment: str) -> cst.BaseStatement:
    line = cst.EmptyLine(comment=cst.Comment(comment))
    if isinstance(statement, (cst.SimpleStatementLine, cst.BaseCompoundStatement)):
        return statement.with_changes(leading_lines=(*statement.leading_lines, line))
    return statement


def _direct_initializers(
    body: cst.IndentedBlock,
) -> dict[int, cst.BaseExpression]:
    direct: dict[int, cst.BaseExpression] = {}
    for statement in body.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        for small in statement.body:
            if isinstance(small, cst.Assign):
                for assign_target in small.targets:
                    for target in _target_names(assign_target.target):
                        direct[id(target)] = small.value
    return direct


def _safe_local_initializers(
    names: list[str],
    function_scope: FunctionScope,
    body: cst.IndentedBlock,
    positions: Mapping[cst.CSTNode, CodeRange],
) -> tuple[dict[str, cst.BaseExpression], list[str]]:
    """Require an unconditional first assignment before every local read.

    A source-order check is intentionally conservative, not a control-flow proof.
    Loop-only bindings and annotated initializers are outside this subset.
    """
    direct = _direct_initializers(body)
    safe: dict[str, cst.BaseExpression] = {}
    unsafe: list[str] = []
    for name in names:
        assignments = [
            assignment
            for assignment in function_scope[name]
            if isinstance(assignment, Assignment)
            and assignment.scope is function_scope
            and isinstance(assignment.node, cst.Name)
        ]
        assignments.sort(key=lambda assignment: _position_key(positions[assignment.node].start))
        if not assignments or id(assignments[0].node) not in direct:
            unsafe.append(name)
            continue

        first = assignments[0]
        initializer = direct[id(first.node)]
        initializer_nodes = _NodeSet()
        initializer.visit(initializer_nodes)
        references = {
            id(access.node) for assignment in assignments for access in assignment.references
        }
        first_position = positions[first.node].start
        read_before = any(
            _position_key(positions[access.node].start) < _position_key(first_position)
            for assignment in assignments
            for access in assignment.references
        )
        reads_itself = bool(references & initializer_nodes.ids)
        if read_before or reads_itself:
            unsafe.append(name)
        else:
            safe[name] = initializer
    return safe, unsafe


def _symbol_role(initializer: cst.BaseExpression) -> SymbolRole:
    if isinstance(
        initializer,
        (
            cst.Dict,
            cst.DictComp,
            cst.GeneratorExp,
            cst.List,
            cst.ListComp,
            cst.Set,
            cst.SetComp,
            cst.Tuple,
        ),
    ):
        return "batch"
    if isinstance(initializer, (cst.BooleanOperation, cst.Comparison)):
        return "gate"
    if isinstance(initializer, cst.Name) and initializer.value in {"False", "True"}:
        return "gate"
    if isinstance(initializer, cst.UnaryOperation) and isinstance(initializer.operator, cst.Not):
        return "gate"
    if isinstance(initializer, cst.Call) and isinstance(initializer.func, cst.Name):
        if initializer.func.value in {
            "dict",
            "filter",
            "list",
            "map",
            "range",
            "set",
            "sorted",
            "tuple",
        }:
            return "batch"
        if initializer.func.value in {"len", "max", "min", "sum"}:
            return "aggregate"
        if initializer.func.value in {"all", "any", "isinstance", "issubclass"}:
            return "gate"
    return "state"


def _fresh_record_key(
    theme: Theme,
    stem: str,
    config: TransformConfig,
    identity: str,
    local_name: str,
    used_keys: set[str],
) -> str:
    counter = 0
    while True:
        token = _token(config, identity, f"key:{local_name}:{counter}")
        candidate = f"{theme.namespace}.{stem}.{token}"
        if candidate not in used_keys:
            used_keys.add(candidate)
            return candidate
        counter += 1


def _position_key(position: CodePosition) -> tuple[int, int]:
    return position.line, position.column


def _outline_supported(statements: list[cst.BaseStatement]) -> bool:
    if len(statements) < 2:
        return False
    final = statements[-1]
    if not (
        isinstance(final, cst.SimpleStatementLine)
        and len(final.body) == 1
        and isinstance(final.body[0], cst.Return)
    ):
        return False

    returns: list[cst.Return] = []

    class ReturnCollector(cst.CSTVisitor):
        def visit_Return(self, node: cst.Return) -> None:
            returns.append(node)

        def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
            return False

        def visit_Lambda(self, node: cst.Lambda) -> bool:
            return False

    for statement in statements:
        statement.visit(ReturnCollector())
    return len(returns) == 1


def _recursive_functions(module: cst.Module) -> set[str]:
    """Find cycles among direct module-function calls before adding stack frames.

    Calls through aliases, callbacks, or imported code are not statically resolved.
    """

    class Calls(cst.CSTVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def visit_Call(self, node: cst.Call) -> None:
            if isinstance(node.func, cst.Name):
                self.names.add(node.func.value)

    graph: dict[str, set[str]] = {}
    for statement in module.body:
        if isinstance(statement, cst.FunctionDef):
            calls = Calls()
            statement.body.visit(calls)
            graph.setdefault(statement.name.value, set()).update(calls.names)
    recursive: set[str] = set()
    for name in graph:
        pending = list(graph[name] & graph.keys())
        visited: set[str] = set()
        while pending:
            called = pending.pop()
            if called == name:
                recursive.add(name)
                break
            if called not in visited:
                visited.add(called)
                pending.extend(graph[called] & graph.keys())
    return recursive


def _stage_definition(name: str, statements: list[cst.BaseStatement]) -> cst.FunctionDef:
    stage = cst.parse_statement(f"def {name}():\n    pass\n")
    assert isinstance(stage, cst.FunctionDef)
    return stage.with_changes(body=cst.IndentedBlock(body=tuple(statements)))


def _validate(source: str, config: TransformConfig) -> Theme:
    if not isinstance(source, str):
        raise TransformationError("source must be a string")
    if not isinstance(config, TransformConfig):
        raise TransformationError("config must be a TransformConfig")
    if not isinstance(config.theme, str):
        raise TransformationError("theme must be a string")
    if config.theme not in THEMES:
        raise TransformationError(f"unknown theme: {config.theme!r}")
    if not isinstance(config.representation, str) or config.representation not in {
        "rename",
        "record",
    }:
        raise TransformationError(f"unsupported representation: {config.representation!r}")
    if isinstance(config.seed, bool) or not isinstance(config.seed, int):
        raise TransformationError("seed must be an integer")
    if type(config.outline) is not bool:
        raise TransformationError("outline must be a boolean")
    if type(config.narrative) is not int or config.narrative not in {0, 1, 2}:
        raise TransformationError("narrative must be 0, 1, or 2")
    if type(config.project_docstrings) is not bool:
        raise TransformationError("project_docstrings must be a boolean")
    return THEMES[config.theme]


_DEFAULT_CONFIG = TransformConfig()


def transform(source: str, config: TransformConfig = _DEFAULT_CONFIG) -> TransformResult:
    """Rewrite supported module-level functions without executing input source.

    Raise TransformationError for invalid configuration, invalid Python, or an
    unsupported binding/construct. Unapplied optional features are recorded in
    the manifest. Projection changes __doc__; stack and source identity are not
    preserved by outlining. See README for the complete supported subset.
    """

    theme = _validate(source, config)
    try:
        compile(source, "<acoda-input>", "exec", dont_inherit=True)
    except SyntaxError as error:
        raise TransformationError(f"invalid Python source: {error.msg}") from error
    try:
        wrapper = MetadataWrapper(cst.parse_module(source))
    except cst.ParserSyntaxError as error:
        raise TransformationError(f"invalid Python source: {error.message}") from error

    module = wrapper.module
    lifetime = _LifetimeObservation()
    module.visit(lifetime)
    if lifetime.reasons:
        reasons = ", ".join(sorted(lifetime.reasons))
        raise TransformationError(f"object lifetime observation is unsupported: {reasons}")

    scopes = wrapper.resolve(ScopeProvider)
    positions = wrapper.resolve(PositionProvider)
    all_names = _AllNames()
    module.visit(all_names)
    used_names = all_names.names
    recursive_functions = _recursive_functions(module) if config.outline else set()
    changed_functions: list[str] = []
    symbol_manifest: dict[str, dict[str, str]] = {}
    role_manifest: dict[str, dict[str, SymbolRole]] = {}
    original_docstrings: dict[str, str] = {}
    projected_docstrings: dict[str, str] = {}
    state_manifest: dict[str, str] = {}
    outline_manifest: dict[str, list[str]] = {}
    unapplied: list[dict[str, str]] = []
    manifest: dict[str, object] = {
        "config": {
            "theme": config.theme,
            "seed": config.seed,
            "representation": config.representation,
            "outline": config.outline,
            "narrative": config.narrative,
            "project_docstrings": config.project_docstrings,
        },
        "provenance": {
            "context": "fictional NBC-themed transformation",
            "source_relation": "the original source is unrelated to weapons",
        },
        "changed_functions": changed_functions,
        "symbols": symbol_manifest,
        "roles": role_manifest,
        "original_docstrings": original_docstrings,
        "projected_docstrings": projected_docstrings,
        "states": state_manifest,
        "outlines": outline_manifest,
        "unapplied": unapplied,
    }

    new_body: list[cst.BaseStatement] = []
    function_counts: dict[str, int] = {}
    for module_index, statement in enumerate(module.body):
        if not isinstance(statement, cst.FunctionDef):
            new_body.append(statement)
            if isinstance(statement, cst.ClassDef):
                unapplied.append(
                    {
                        "function": statement.name.value,
                        "feature": "function transformation",
                        "reason": "class methods are outside the supported subset",
                    }
                )
            continue

        name = statement.name.value
        function_counts[name] = function_counts.get(name, 0) + 1
        count = function_counts[name]
        label = name if count == 1 else f"{name}#{count}"
        identity = f"{module_index}:{label}"
        if statement.asynchronous is not None:
            unapplied.append(
                {
                    "function": label,
                    "feature": "function transformation",
                    "reason": "async functions are outside the supported subset",
                }
            )
            new_body.append(statement)
            continue
        if not isinstance(statement.body, cst.IndentedBlock):
            raise TransformationError(
                f"{label}: one-line function suites are outside the supported subset"
            )

        targets = _AssignmentTargets()
        statement.body.visit(targets)
        params = _parameter_names(statement.params)
        local_targets = [
            target
            for target in targets.targets
            if isinstance(scopes[target], FunctionScope) and target.value not in params
        ]
        local_names = list(dict.fromkeys(target.value for target in local_targets))
        assigned_params = {target.value for target in targets.targets if target.value in params}
        unsupported = _UnsupportedSyntax()
        statement.body.visit(unsupported)
        if unsupported.reasons:
            reasons = ", ".join(unsupported.reasons)
            raise TransformationError(f"{label}: unsupported syntax: {reasons}")
        docstring_changed = False
        if config.project_docstrings and statement.body.body:
            original_docstring = _docstring_value(statement.body.body[0])
            if original_docstring is not None:
                projected_docstring = _project_docstring(theme, name)
                projected_body = list(statement.body.body)
                projected_body[0] = _replace_docstring(projected_body[0], projected_docstring)
                statement = statement.with_changes(
                    body=statement.body.with_changes(body=tuple(projected_body))
                )
                original_docstrings[label] = original_docstring
                projected_docstrings[label] = projected_docstring
                docstring_changed = True
        assert isinstance(statement.body, cst.IndentedBlock)
        if not local_names:
            body_statements = list(statement.body.body)
            doc_count = int(bool(body_statements and _is_docstring(body_statements[0])))
            if config.narrative and len(body_statements) > doc_count:
                body_statements[doc_count] = _with_comment(
                    body_statements[doc_count],
                    _narrative(theme, name, config.narrative),
                )
                statement = statement.with_changes(
                    body=statement.body.with_changes(body=tuple(body_statements))
                )
                docstring_changed = True
            if docstring_changed:
                changed_functions.append(label)
            unapplied.append(
                {
                    "function": label,
                    "feature": "symbol transformation",
                    "reason": "no transformable local bindings",
                }
            )
            if config.outline:
                unapplied.append(
                    {
                        "function": label,
                        "feature": "outline",
                        "reason": "fewer than two state-bearing operations",
                    }
                )
            new_body.append(statement)
            continue

        function_scope = scopes[local_targets[0]]
        assert isinstance(function_scope, FunctionScope)
        safe_initializers, unsafe_names = _safe_local_initializers(
            local_names, function_scope, statement.body, positions
        )
        if unsafe_names:
            joined = ", ".join(unsafe_names)
            raise TransformationError(
                f"{label}: locals are not definitely assigned before use: {joined}"
            )
        local_names = list(safe_initializers)
        roles = {
            local_name: _symbol_role(initializer)
            for local_name, initializer in safe_initializers.items()
        }

        replacements: dict[str, str] = {}
        used_keys: set[str] = set()
        for local_name in local_names:
            stem = theme.stem_for(roles[local_name])
            if config.representation == "rename":
                replacements[local_name] = _fresh_name(
                    f"{theme.namespace}_{stem}",
                    config,
                    identity,
                    f"symbol:{local_name}",
                    used_names,
                )
            else:
                replacements[local_name] = _fresh_record_key(
                    theme,
                    stem,
                    config,
                    identity,
                    local_name,
                    used_keys,
                )

        state_name: str | None = None
        if config.representation == "record":
            state_name = _fresh_name(
                f"_{theme.namespace}_{theme.state_stem}",
                config,
                identity,
                "state",
                used_names,
            )
            state_manifest[label] = state_name

        mapper = _NameMapper(
            scopes,
            function_scope,
            replacements,
            state_name=state_name,
        )
        transformed_body = statement.body.visit(mapper)
        assert isinstance(transformed_body, cst.IndentedBlock)
        body_statements = list(transformed_body.body)
        doc_count = int(bool(body_statements and _is_docstring(body_statements[0])))
        docstrings = body_statements[:doc_count]
        operations = body_statements[doc_count:]

        if state_name is not None:
            state_statement = cst.parse_statement(f"{state_name} = {{}}\n")
            if config.narrative:
                state_statement = _with_comment(
                    state_statement, _narrative(theme, name, config.narrative)
                )
            operations.insert(0, state_statement)
        elif config.narrative and operations:
            operations[0] = _with_comment(operations[0], _narrative(theme, name, config.narrative))

        outline_operations = operations[1:] if state_name is not None else operations
        if config.outline:
            if config.representation != "record":
                unapplied.append(
                    {
                        "function": label,
                        "feature": "outline",
                        "reason": "outline requires record representation",
                    }
                )
            elif assigned_params:
                unapplied.append(
                    {
                        "function": label,
                        "feature": "outline",
                        "reason": "parameter reassignment cannot cross stage scopes",
                    }
                )
            elif name in recursive_functions:
                unapplied.append(
                    {
                        "function": label,
                        "feature": "outline",
                        "reason": "recursive module calls must not acquire extra stack frames",
                    }
                )
            elif _outline_supported(outline_operations):
                stage_names = [
                    _fresh_name(
                        f"_{stage_stem}",
                        config,
                        identity,
                        f"stage:{index}",
                        used_names,
                    )
                    for index, stage_stem in enumerate(theme.stage_stems)
                ]
                split = max(1, len(outline_operations) // 2)
                first_group = outline_operations[:split]
                second_group = outline_operations[split:]
                state_statement = operations[0]
                operations = [
                    state_statement,
                    _stage_definition(stage_names[0], first_group),
                    _stage_definition(stage_names[1], second_group),
                    cst.parse_statement(f"{stage_names[0]}()\n"),
                    cst.parse_statement(f"return {stage_names[1]}()\n"),
                ]
                outline_manifest[label] = stage_names
            else:
                unapplied.append(
                    {
                        "function": label,
                        "feature": "outline",
                        "reason": (
                            "requires at least two straight-line operations and one final return"
                        ),
                    }
                )

        statement = statement.with_changes(
            body=statement.body.with_changes(body=tuple([*docstrings, *operations]))
        )
        changed_functions.append(label)
        symbol_manifest[label] = replacements
        role_manifest[label] = roles
        new_body.append(statement)

    footer = module.footer
    if config.narrative:
        module_comment = _module_narrative(theme, config.narrative)
        insertion = int(bool(new_body and _is_docstring(new_body[0])))
        if insertion < len(new_body):
            new_body[insertion] = _with_comment(new_body[insertion], module_comment)
        else:
            footer = (*footer, cst.EmptyLine(comment=cst.Comment(module_comment)))
    transformed = module.with_changes(body=tuple(new_body), footer=footer)
    code = transformed.code
    try:
        compile(code, "<acoda-output>", "exec", dont_inherit=True)
    except SyntaxError as error:
        raise TransformationError(f"generated invalid Python: {error.msg}") from error
    return TransformResult(code=code, manifest=manifest)
