"""Small helpers for testing the existing standalone pipeline scripts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


def load_pipeline_module(module_name: str, script_name: str) -> ModuleType:
    """Load a pipeline script as a module without running its main function."""

    script_path = PROJECT_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load pipeline script: {script_path}")

    module = importlib.util.module_from_spec(spec)
    # Dataclasses and postponed annotations expect the module to be registered
    # while its top-level definitions are executed.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
