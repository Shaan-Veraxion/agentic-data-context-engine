"""Per-provider request and token limits (free tiers allow a few requests and a few thousand tokens per minute).

A sliding one-minute window per provider: before a call, wait until one more request and its estimated tokens fit.
Tokens are estimated from the prompt (about 4 characters per token) plus the output budget, then corrected with
the usage the provider reports. Provider-side 429 responses are still retried by the SDKs with back-off.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


class RateLimiter:
    def __init__(self, rpm: int | None, tpm: int | None, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] = asyncio.sleep) -> None:
        self.rpm, self.tpm = rpm, tpm
        self._clock, self._sleep = clock, sleep
        self._events: deque[list[float]] = deque()  # [time, tokens]
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= 60.0:
            self._events.popleft()

    def wait_time(self, tokens: int) -> float:
        """Seconds to wait before a call of `tokens` fits in the window (0 when it fits now)."""
        now = self._clock()
        self._prune(now)
        waits = [0.0]
        if self.rpm is not None and len(self._events) >= self.rpm:
            waits.append(60.0 - (now - self._events[len(self._events) - self.rpm][0]))
        if self.tpm is not None:
            budget = min(tokens, self.tpm)  # a call bigger than the whole budget waits for an empty window
            used = sum(e[1] for e in self._events)
            for t, n in self._events:
                if used + budget <= self.tpm:
                    break
                used -= n
                waits.append(60.0 - (now - t))
        return max(waits)

    async def acquire(self, tokens: int) -> list[float]:
        """Wait for room, record the call, and return its window entry (correct it later with `settle`)."""
        async with self._lock:
            while (w := self.wait_time(tokens)) > 0:
                await self._sleep(w + 0.05)  # type: ignore[misc]
            entry = [self._clock(), float(tokens)]
            self._events.append(entry)
            return entry

    @staticmethod
    def settle(entry: list[float], actual_tokens: int) -> None:
        """Correct the estimate upwards only: providers such as Groq count the requested output budget toward the
        minute's tokens, not just the tokens produced, so crediting back unused budget leads to 429s."""
        entry[1] = max(entry[1], float(actual_tokens))
