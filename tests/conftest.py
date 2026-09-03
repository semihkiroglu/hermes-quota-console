from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
PLUGIN_API_PATH = REPOSITORY_ROOT / "dashboard" / "plugin_api.py"


@pytest.fixture
def load_fixture():
    def load(relative_path: str):
        return json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def plugin_api():
    """Load the plugin module from this repository without Hermes state."""
    module_name = f"quota_console_test_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_API_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for loaded_name in tuple(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(f"{module_name}."):
                sys.modules.pop(loaded_name, None)
