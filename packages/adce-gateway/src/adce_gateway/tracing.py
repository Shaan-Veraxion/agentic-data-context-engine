"""Langfuse tracing for gateway calls (optional: the `tracing` extra and LANGFUSE_* settings).

Each live model call becomes a Langfuse generation with route, provider, model, token usage and cost. Inputs are
what the gateway sent, which the privacy guard has already checked (no data rows). Replayed and memoized answers
are not traced: they cost nothing and were traced when recorded.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def langfuse_tracer() -> Any | None:
    """A tracer for ModelGateway, or None when Langfuse is not configured or not installed."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse  # pyright: ignore[reportMissingImports]  (optional extra)
    except ImportError:
        log.info("LANGFUSE_* is set but the langfuse package is not installed (adce-gateway[tracing])")
        return None
    client = Langfuse()

    def trace(result: Any, inputs: dict[str, Any]) -> None:
        try:
            gen = client.start_generation(
                name=f"gateway.{result.route}", input=inputs, output=result.output.model_dump(mode="json"),
                model=result.model, version=result.prompt_version,
                metadata={"provider": result.provider, "key": result.key, "attempts": result.attempts,
                          "latency_ms": result.latency_ms, "priced": result.priced},
                usage_details={"input": result.usage.input_tokens, "output": result.usage.output_tokens,
                               "cache_read_input_tokens": result.usage.cache_read_tokens},
                cost_details={"total": result.cost_usd} if result.priced else None)
            gen.end()
        except Exception:  # tracing must never fail a model call
            log.exception("Langfuse trace failed")

    return trace
