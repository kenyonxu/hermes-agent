#!/usr/bin/env python3
"""AST scanner: locate sync SessionDB calls within async function bodies + unwrap anti-pattern.

Usage:
    python scripts/audit_sync_sessiondb.py gateway/ cron/ tools/
    # exit code 1 = violations found (for CI gate)
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

# Known SessionDB method name prefixes (synchronous API surface)
SYNC_METHOD_PREFIXES = (
    "get_", "list_", "find_", "create_", "end_", "open_", "record_", "bind_",
    "is_", "rewind_", "load_", "set_", "update_", "delete_", "append_",
    "prune_", "archive_", "vacuum", "rotate", "release_", "acquire_",
)
SAFE_MARKER = "SYNC_SESSIONDB_SAFE"


@dataclass
class Finding:
    filename: str
    lineno: int
    detail: str

    def __str__(self) -> str:
        return f"{self.filename}:{self.lineno} {self.detail}"


def _receiver_is_db_like(value: ast.expr) -> bool:
    """True when the call receiver looks like a SessionDB handle (name contains 'db')."""
    if isinstance(value, ast.Name):
        return "db" in value.id.lower()
    if isinstance(value, ast.Attribute):
        return "db" in value.attr.lower()
    return False


def _is_sync_db_call(node: ast.Call) -> bool:
    """node.func is an x.method(...), method matches a sync prefix, and x is a db-like handle."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    name = func.attr
    if not any(name.startswith(p) or name == p.rstrip("_") for p in SYNC_METHOD_PREFIXES):
        return False
    return _receiver_is_db_like(func.value)


def _uses_to_thread(node: ast.AST) -> bool:
    """True when the function body contains an asyncio.to_thread(...) call anywhere."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Attribute) and f.attr == "to_thread":
            return True
        if isinstance(f, ast.Name) and f.id == "to_thread":
            return True
    return False


def _line_has_safe_marker(source_lines: List[str], lineno: int) -> bool:
    if 0 < lineno <= len(source_lines):
        return SAFE_MARKER in source_lines[lineno - 1]
    return False


def scan_source(source: str, filename: str = "<src>") -> List[Finding]:
    """Scan a single source string, returning all violation Findings."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    lines = source.splitlines()
    findings: List[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        is_async = isinstance(node, ast.AsyncFunctionDef)
        func_uses_to_thread = _uses_to_thread(node)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if _line_has_safe_marker(lines, sub.lineno):
                continue
            # Special rule: getattr(x, "_db", x) unwrap
            if (
                isinstance(sub.func, ast.Name)
                and sub.func.id == "getattr"
                and len(sub.args) >= 2
                and isinstance(sub.args[1], ast.Constant)
                and sub.args[1].value == "_db"
                and is_async
                and not func_uses_to_thread
            ):
                findings.append(Finding(
                    filename, sub.lineno,
                    "AsyncSessionDB unwrap — AsyncSessionDB default would be ineffective for this call site, use async facade",
                ))
                continue
            # Sync DB method call inside an async function body
            if is_async and _is_sync_db_call(sub):
                findings.append(Finding(
                    filename, sub.lineno,
                    f"async-context sync SessionDB call ({sub.func.attr})",
                ))
    return findings


def scan_path(root: Path) -> Iterable[Finding]:
    for py in root.rglob("*.py"):
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for f in scan_source(source, filename=str(py)):
            yield f


def main(argv: List[str]) -> int:
    roots = [Path(p) for p in argv[1:]] or [Path("gateway"), Path("cron"), Path("tools")]
    findings: List[Finding] = []
    for r in roots:
        if not r.exists():
            print(f"warn: {r} does not exist, skipping", file=sys.stderr)
            continue
        findings.extend(scan_path(r))
    for f in findings:
        print(f"RED {f}")
    print(f"\nTotal {len(findings)} violation(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
