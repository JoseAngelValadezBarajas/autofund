"""Deterministic monitoring fixture; no execution/client imports or writes."""

import json
from pathlib import Path

from .models import RuntimeView


class DemoMicroRuntime:
    def __init__(self) -> None:
        self.frames = json.loads((Path(__file__).with_name("fixtures") / "micro_live.json").read_text(encoding="utf-8"))

    def snapshot(self, step: int = 0) -> RuntimeView:
        return RuntimeView.model_validate(self.frames[min(step, len(self.frames) - 1)])
