from __future__ import annotations

import ast
import inspect

from dubbing_pipeline.security import policy


def _strip_docstrings(node: ast.AST) -> None:
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.body and isinstance(node.body[0], ast.Expr):
            first = node.body[0].value
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                node.body = node.body[1:]
    for child in ast.iter_child_nodes(node):
        _strip_docstrings(child)


def _policy_ast() -> ast.AST:
    src = inspect.getsource(policy)
    tree = ast.parse(src)
    _strip_docstrings(tree)
    return tree


def _has_not_implemented_reference(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "NotImplementedError":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "NotImplementedError":
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "NotImplementedError" in node.value:
                return True
    return False


def _raises_not_implemented(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
        if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
            if exc.func.id == "NotImplementedError":
                return True
    return False


def test_policy_module_has_no_placeholders() -> None:
    tree = _policy_ast()
    assert not _has_not_implemented_reference(tree), "Policy module contains NotImplementedError"
    assert not _raises_not_implemented(tree), "Policy functions raise NotImplementedError"
