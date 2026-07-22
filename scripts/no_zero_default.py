#!/usr/bin/env python3
"""Reject code patterns that can silently turn a missing observation into zero."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
PATHS = (
    ROOT / "src/rpc_state_indexer/collectors",
    ROOT / "src/rpc_state_indexer/core/census.py",
    ROOT / "src/rpc_state_indexer/evm/decoding.py",
)


def files() -> list[Path]:
    output: list[Path] = []
    for path in PATHS:
        output.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return output


def is_zero(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value == 0


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    output: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(is_zero(value) for value in node.values):
                output.append(f"{path}:{node.lineno}: boolean `or 0` is forbidden")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and len(node.args) >= 2 and is_zero(node.args[1]):
                output.append(f"{path}:{node.lineno}: `.get(..., 0)` is forbidden")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 3
            and is_zero(node.args[2])
        ):
            output.append(f"{path}:{node.lineno}: `getattr(..., 0)` is forbidden")
    return output


def main() -> int:
    failures = [failure for path in files() for failure in violations(path)]
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"no-zero-default: checked {len(files())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
