"""Named routes: each fixes the model, effort, output size and prompt version for one kind of AI task.

Agents ask for a route, never for a model. Changing a model is a route change, gated by the gold-set evaluation.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

# Versioned prompts live in the repository's prompts/ folder; images set ADCE_PROMPTS_DIR (see deploy/docker).
PROMPTS = Path(os.environ.get("ADCE_PROMPTS_DIR") or Path(__file__).resolve().parents[4] / "prompts")


class Route(BaseModel):
    name: str
    model: str
    effort: str | None = None  # low | medium | high | xhigh | max (Opus 5.5 / Sonnet 5.5)
    max_tokens: int = 8000
    prompt_version: str = "v1"
    rows_allowed: bool = False  # starting stage: no route may receive row-level data

    def system_prompt(self) -> str:
        p = PROMPTS / self.name / f"{self.prompt_version}.md"
        if not p.exists():
            raise FileNotFoundError(f"prompt {p} is missing for route {self.name}")
        return p.read_text(encoding="utf-8")


OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-4-5"  # architecture choice; evaluate newer Haiku models on the gold set before switching

ROUTES: dict[str, Route] = {r.name: r for r in [
    Route(name="translate_statement", model=SONNET, effort="low", max_tokens=8000),
    Route(name="translate_expression", model=SONNET, effort="medium", max_tokens=8000),
    Route(name="repair_sql", model=OPUS, effort="medium", max_tokens=12000),
    Route(name="diagnose_mismatch", model=OPUS, effort="high", max_tokens=16000),
    Route(name="parse_export", model=SONNET, effort="medium", max_tokens=16000),
    Route(name="classify_file", model=HAIKU, effort=None, max_tokens=1000),
]}

# USD per million tokens (input, output, cache read). Vertex AI bills at Google's rates; confirm per region (Q-25).
PRICES: dict[str, tuple[float, float, float]] = {
    OPUS: (4.0, 20.0, 0.20),
    SONNET: (2.0, 10.0, 0.20),
    HAIKU: (1.0, 5.0, 0.10),
}
