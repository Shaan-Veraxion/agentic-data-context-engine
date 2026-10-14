"""Evaluate an LLM change: run the LLM cases live in two or more arms and compare (ADR-10: ranges over repeated
runs, never single runs).

Each trial starts clean: no recorded answers are reused and the translation memory is empty, so every arm pays for
its own model calls. Arms are interleaved (trial 1: case A arm 1, case A arm 2, case B arm 1, ...) so quota and
time-of-day effects fall on both. A trial the provider could not serve (rate or daily limit) is marked invalid,
not failed.

    uv run adce-bench eval --arms plain,tools --trials 2         # results in eval/results/
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path
from typing import Any

from .cases import ROOT, Case, all_cases
from .runner import Runner, Variant

RESULTS = ROOT / "eval" / "results"
DEFAULT_CASES = ("llm/", "apps/longtail_local", "apps/rulefix_local")
ARMS = {"plain": False, "tools": True}


def eval_cases(prefixes: tuple[str, ...] = DEFAULT_CASES) -> list[Case]:
    return [c for c in all_cases() if not c.private and any(c.id.startswith(p) for p in prefixes)]


async def run_trial(case: Case, arm: str) -> dict[str, Any]:
    runner = Runner(case, Variant("live", "full", ARMS[arm]))
    runner.recordings = Path(tempfile.mkdtemp(prefix="adce-eval-rec-"))  # nothing reused between trials
    r = await runner.run()
    calls = r.gateway.get("calls", [])
    tr = next((s for s in r.steps if s["step"] == "translate" and not s.get("error")), None)
    outcomes = []
    if tr is not None and runner.state.get("translation_key"):
        outcomes = json.loads(runner.store.get(runner.state["translation_key"])).get("ai_outcomes", [])
    errors = [c["error"] for c in calls if c.get("error")]
    quota = any(s in e for e in errors for s in ("RateLimit", "rate_limit", "429", "every provider failed"))
    return {"case": case.id, "arm": arm, "status": r.status, "valid": not quota or r.status == "pass",
            "correct": r.status == "pass", "seconds": round(r.seconds, 1),
            "model_calls": sum(1 for c in calls if not c.get("error")), "failed_calls": len(errors),
            "calls": [{"route": c["route"], "ok": not c.get("error"), "turns": c.get("turns"),
                       "tools": [t["tool"] + (f" ({t['error']})" if t.get("error") else "")
                                 for t in c.get("tools") or []],
                       "error": (c.get("error") or "")[:200]} for c in calls],
            "tool_calls": sum(len(c.get("tools") or []) for c in calls),
            "turns": sum(c.get("turns") or 0 for c in calls),
            "input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
            "output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
            "outcomes": [{k: o.get(k) for k in ("table", "statement", "status", "attempts", "model_calls",
                                                 "tool_calls")} for o in outcomes],
            "failed_checks": [f"[{c.section}] {c.name}" for c in r.checks if not c.ok][:10],
            "errors": [e[:300] for e in errors][:5],
            "tools_used": sorted({t["tool"] for c in calls for t in c.get("tools") or []})}


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm and r["valid"]]
        n = len(mine)
        out[arm] = {"valid_trials": n, "invalid_trials": sum(1 for r in rows if r["arm"] == arm and not r["valid"]),
                    "correct": sum(r["correct"] for r in mine),
                    "correct_rate": round(sum(r["correct"] for r in mine) / n, 3) if n else None,
                    "model_calls_per_trial": round(sum(r["model_calls"] for r in mine) / n, 2) if n else None,
                    "tool_calls_per_trial": round(sum(r["tool_calls"] for r in mine) / n, 2) if n else None,
                    "tokens_per_trial": round(sum(r["input_tokens"] + r["output_tokens"] for r in mine) / n)
                    if n else None,
                    "seconds_per_trial": round(sum(r["seconds"] for r in mine) / n, 1) if n else None,
                    "per_case": {c: f"{sum(r['correct'] for r in mine if r['case'] == c)}/"
                                    f"{sum(1 for r in mine if r['case'] == c)}"
                                 for c in sorted({r['case'] for r in rows})}}
    return out


def write(rows: list[dict[str, Any]], name: str) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"{name}-{dt.datetime.now().strftime('%Y%m%d-%H%M')}.json"
    p.write_text(json.dumps({"summary": summarise(rows), "trials": rows}, indent=1, default=str), encoding="utf-8")
    return p
