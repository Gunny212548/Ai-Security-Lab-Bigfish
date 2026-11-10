"""SPEC §6.7: per team per challenge, one in-flight request + RATE_LIMIT_PER_MIN requests per minute."""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque

WINDOW_S = 60.0


class RateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute = per_minute
        self.clock = clock
        self._locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._hits: dict[tuple, deque[float]] = defaultdict(deque)
        self.in_flight = 0  # all teams, for the queue indicator

    def lock(self, key: tuple) -> asyncio.Lock:
        return self._locks[key]

    def hit(self, key: tuple) -> int | None:
        """Record a request. Returns seconds to wait if over the limit (request not recorded), else None."""
        now = self.clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= WINDOW_S:
            hits.popleft()
        if self.per_minute > 0 and len(hits) >= self.per_minute:
            return max(1, math.ceil(WINDOW_S - (now - hits[0])))
        hits.append(now)
        return None


def slow_down_message(seconds: int) -> str:
    return f"Slow down 🙂 try again in {seconds} seconds"


BUSY_IN_FLIGHT_MESSAGE = "Slow down 🙂 wait for the current reply first"
