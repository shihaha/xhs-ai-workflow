"""Fail a release when permanent deletion escapes the reviewed cleanup boundary."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path


_APPROVED_DELETE_BOUNDARIES = {
    Path("backend/app/features/content/cleanup.py"),
    Path("backend/app/features/content/export.py"),
}
_PROJECT_DELETE_HELPERS = {
    "remove_contained_regular",
    "_delete_open_file",
    "_set_delete_disposition",
    "SetFileInformationByHandle",
}


@dataclass(frozen=True)
class BoundaryFinding:
    path: str
    line: int
    category: str


def scan_python_boundaries(root: Path) -> list[BoundaryFinding]:
    app_root = root / "backend" / "app"
    findings: list[BoundaryFinding] = []
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        except (OSError, UnicodeError, SyntaxError) as error:
            line = error.lineno if isinstance(error, SyntaxError) and error.lineno else 1
            findings.append(BoundaryFinding(relative.as_posix(), line, "scan_failed"))
            continue
        aliases = _delete_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in {"delete", "patch"}
                    ):
                        findings.append(
                            BoundaryFinding(
                                relative.as_posix(),
                                decorator.lineno,
                                "destructive_http_route",
                            )
                        )
            if (
                relative not in _APPROVED_DELETE_BOUNDARIES
                and isinstance(node, ast.Call)
                and _is_permanent_delete(node.func, aliases)
            ):
                findings.append(
                    BoundaryFinding(
                        relative.as_posix(), node.lineno, "permanent_delete"
                    )
                )
    return sorted(findings, key=lambda item: (item.path, item.line, item.category))


def _delete_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name in {"os", "shutil"}:
                    aliases.add(name.asname or name.name)
        elif isinstance(node, ast.ImportFrom) and node.module in {"os", "shutil"}:
            for name in node.names:
                if name.name in {"unlink", "remove", "rmdir", "removedirs", "rmtree"}:
                    aliases.add(name.asname or name.name)
        elif isinstance(node, ast.ImportFrom):
            for name in node.names:
                if name.name in _PROJECT_DELETE_HELPERS:
                    aliases.add(name.asname or name.name)
    return aliases


def _is_permanent_delete(function: ast.expr, aliases: set[str]) -> bool:
    if isinstance(function, ast.Name):
        return (
            function.id in _PROJECT_DELETE_HELPERS
            or (function.id in aliases and function.id not in {"os", "shutil"})
        )
    if not isinstance(function, ast.Attribute):
        return False
    if function.attr in {
        "unlink",
        "rmdir",
        "rmtree",
        "DeleteFileW",
        "RemoveDirectoryW",
    } | _PROJECT_DELETE_HELPERS:
        return True
    return (
        function.attr in {"remove", "removedirs"}
        and isinstance(function.value, ast.Name)
        and function.value.id in aliases
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    findings = scan_python_boundaries(args.root.resolve())
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.category}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
