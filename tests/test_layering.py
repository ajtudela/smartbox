"""The extracted layers stay Home Assistant-free."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "smartbox"


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_lower_layers_import_no_homeassistant() -> None:
    for layer in ("protocol", "core", "device"):
        sources = sorted((PACKAGE_ROOT / layer).glob("*.py"))
        assert sources, f"{layer} package is missing"
        for path in sources:
            imports = _imports(path)
            assert not any(
                module == "homeassistant" or module.startswith("homeassistant.")
                for module in imports
            ), f"{path} imports Home Assistant"
