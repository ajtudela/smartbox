"""The installed console script and module entry point stay wired."""

from __future__ import annotations

import importlib
from pathlib import Path
import tomllib

import smartbox.cli as cli_pkg

ROOT = Path(__file__).parents[1]


def test_console_script_resolves_to_a_callable() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    target = data["project"]["scripts"]["smartbox"]
    module_name, _, attribute = target.partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute))


def test_package_entry_point_and_public_names() -> None:
    assert (ROOT / "src" / "smartbox" / "cli" / "__main__.py").is_file()
    assert set(cli_pkg.__all__) == {"cli", "smartbox"}
    assert callable(cli_pkg.cli)
