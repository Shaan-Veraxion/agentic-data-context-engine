"""Bench and evaluation results into Langfuse: one trace per case and variant, scored.

Used by `adce-bench run --langfuse` (CI does this on every push when the LANGFUSE_* secrets are set). Each trace is
named `bench:<case>` and tagged with the run name (the git commit in CI), the AI mode and the data variant, so
Langfuse shows how a case's result changes across prompt, model and rule changes. Scores:

- `pass`: 1 for pass or xfail (an expected failure), 0 for fail, error or xpass;
- `checks_ok`: the share of the case's checks that held.

Nothing is sent without LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, or without the langfuse package
(adce-gateway[tracing]).
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def report(rows: list[dict[str, Any]], run_name: str) -> int:
    """Send the results; returns how many traces were sent (0 when Langfuse is not configured)."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        print("langfuse: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set; nothing sent")
        return 0
    try:
        from langfuse import Langfuse  # pyright: ignore[reportMissingImports]  (optional extra)
    except ImportError:
        print("langfuse: the langfuse package is not installed (adce-gateway[tracing]); nothing sent")
        return 0
    client = Langfuse()
    sent = 0
    for r in rows:
        checks = r.get("checks") or []
        ok = sum(1 for c in checks if c.get("ok"))
        variant = r.get("variant") or {}
        failed = [f"[{c.get('section')}] {c.get('name')}" for c in checks if not c.get("ok")][:50]
        try:
            span = client.start_span(name=f"bench:{r.get('case')}", input={"variant": variant},
                                     output={"status": r.get("status"), "failed": failed},
                                     metadata={"seconds": r.get("seconds"), "run": run_name})
            span.update_trace(tags=[run_name, f"ai={variant.get('ai')}", f"data={variant.get('data')}"],
                              session_id=run_name)
            span.score_trace(name="pass", value=1.0 if r.get("status") in ("pass", "xfail") else 0.0)
            span.score_trace(name="checks_ok", value=ok / len(checks) if checks else 1.0)
            span.end()
            sent += 1
        except Exception:  # noqa: BLE001 - reporting never fails the run
            log.exception("langfuse: could not send %s", r.get("case"))
    client.flush()
    print(f"langfuse: {sent} traces sent (session {run_name})")
    return sent
