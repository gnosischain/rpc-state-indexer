#!/usr/bin/env python3
"""Guard the discovery scanner against catch-and-skip regressions."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PATH = Path(__file__).parents[1] / "src/rpc_state_indexer/core/discovery.py"


def main() -> int:
    source = PATH.read_text()
    tree = ast.parse(source, filename=str(PATH))
    failures: list[str] = []
    for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
        descendants = tuple(ast.walk(handler))
        if any(isinstance(node, ast.Pass) for node in descendants):
            failures.append(f"{PATH}:{handler.lineno}: exception handler contains pass")
        if any(isinstance(node, ast.Continue) for node in descendants) and not any(
            isinstance(node, ast.Raise) for node in descendants
        ):
            failures.append(
                f"{PATH}:{handler.lineno}: exception handler continues without a fail-closed raise"
            )
    lowered = source.casefold()
    for banned in ("failed, skipped", "failed; skipped", "warn: block"):
        if banned in lowered:
            failures.append(f"{PATH}: banned skip marker {banned!r}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("no-silent-rpc-failures: strict discovery handlers verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
