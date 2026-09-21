"""Per-domain rate limiting (SPEC §9)."""

import asyncio
import time
from collections.abc import Awaitable, Callable


class DomainRateLimiter:
    """Ensures at least `interval` seconds between request starts to the same host."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._clock = clock
        self._sleep = sleep
        self._next_allowed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str, interval: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = self._next_allowed.get(host, 0.0) - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_allowed[host] = self._clock() + interval

    def defer(self, host: str, seconds: float) -> None:
        """Push back the next allowed request to `host` (e.g. after a 429)."""
        self._next_allowed[host] = max(self._next_allowed.get(host, 0.0), self._clock() + seconds)
