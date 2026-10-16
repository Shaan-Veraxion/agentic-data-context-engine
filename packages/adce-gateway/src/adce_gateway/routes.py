"""Named routes: each fixes the model tier, effort, output size and prompt version for one kind of AI task.

Agents ask for a route, never for a model. Which provider and model serve a tier is configuration
(config.GatewayConfig); changing it is gated by the gold-set evaluation.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

# Versioned prompts live in the repository's prompts/ folder; images set ADCE_PROMPTS_DIR (see deploy/docker).
PROMPTS = Path(os.environ.get("ADCE_PROMPTS_DIR") or Path(__file__).resolve().parents[4] / "prompts")


class Route(BaseModel):
    name: str
    tier: Literal["strong", "standard", "light"]
    effort: str | None = None  # low | medium | high | xhigh | max; passed on where the model supports it
    max_tokens: int = 8000
    prompt_version: str = "v1"
    rows_allowed: bool = False  # starting stage: no route may receive row-level data

    def system_prompt(self) -> str:
        p = PROMPTS / self.name / f"{self.prompt_version}.md"
        if not p.exists():
            raise FileNotFoundError(f"prompt {p} is missing for route {self.name}")
        return p.read_text(encoding="utf-8")


ROUTES: dict[str, Route] = {r.name: r for r in [
    Route(name="translate_statement", tier="standard", effort="medium", max_tokens=8000, prompt_version="v3"),
    Route(name="translate_expression", tier="standard", effort="medium", max_tokens=8000),
    Route(name="repair_sql", tier="strong", effort="medium", max_tokens=12000, prompt_version="v2"),
    Route(name="diagnose_mismatch", tier="strong", effort="high", max_tokens=16000, prompt_version="v2"),
    Route(name="parse_export", tier="standard", effort="medium", max_tokens=16000),
    Route(name="classify_file", tier="light", effort=None, max_tokens=1000),
    Route(name="map_metric", tier="standard", effort="medium", max_tokens=8000),
    Route(name="explain_gap", tier="standard", effort="medium", max_tokens=6000),
    Route(name="plan_next", tier="standard", effort="low", max_tokens=2000),
]}
