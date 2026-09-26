"""Public-safe Demo mode: mode resolution, credential isolation and the demo dataset.

The package exists so that "is this run allowed to touch money?" is answered in one importable
place rather than re-derived at each entry point. Every public-facing command resolves its mode
through :func:`autofund.demo.mode.resolve_mode`, which fails closed.
"""

from __future__ import annotations

from .dataset import DEMO_DATASET_VERSION, PROVENANCE, generate, is_demo_dataset
from .mode import (
    DEFAULT_MODE,
    MODE_ENV_VAR,
    DemoIsolationError,
    IsolationReport,
    ModeError,
    RuntimeMode,
    assert_demo_is_isolated,
    credential_isolation,
    demo_environment,
    resolve_mode,
)

__all__ = [
    "DEFAULT_MODE",
    "DEMO_DATASET_VERSION",
    "MODE_ENV_VAR",
    "PROVENANCE",
    "DemoIsolationError",
    "IsolationReport",
    "ModeError",
    "RuntimeMode",
    "assert_demo_is_isolated",
    "credential_isolation",
    "demo_environment",
    "generate",
    "is_demo_dataset",
    "resolve_mode",
]
