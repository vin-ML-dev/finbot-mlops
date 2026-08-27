"""Resilience layer — handles failures safely so the gateway degrades instead of
crashing. Two mechanisms, deliberately simple and dependency-free:

  - CircuitBreaker: after N consecutive upstream failures, "open" the circuit and
    fail fast for a cooldown window instead of hammering a sick model. After the
    window, allow one trial request (half-open); success closes it, failure
    re-opens it.
  - retry helper: bounded retries for TRANSIENT errors only, with backoff.

Neither is a second inference engine — they only govern how we call the model."""
from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"        # normal — requests flow
    OPEN = "open"            # tripped — fail fast, don't call upstream
    HALF_OPEN = "half_open"  # trial — allow one probe request


class CircuitOpenError(Exception):
    """Raised when the circuit is open and we fail fast."""


class CircuitBreaker:
    def __init__(self, fail_threshold: int, reset_timeout_s: float) -> None:
        self._fail_threshold = fail_threshold
        self._reset_timeout_s = reset_timeout_s
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def _allow(self) -> bool:
        """Decide whether a call may proceed; move OPEN->HALF_OPEN when cooled."""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if (time.monotonic() - self._opened_at) >= self._reset_timeout_s:
                    self._state = CircuitState.HALF_OPEN
                    return True          # allow a single trial
                return False             # still cooling down
            return True                  # CLOSED or HALF_OPEN

    async def _on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == CircuitState.HALF_OPEN or self._failures >= self._fail_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        if not await self._allow():
            raise CircuitOpenError("circuit open — upstream model unhealthy")
        try:
            result = await fn()
        except Exception:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    backoff_s: float,
    retry_on: tuple[type[Exception], ...],
) -> T:
    """Call fn, retrying only on the given transient exception types, with linear
    backoff. Bounded — never retries forever (that makes overload worse)."""
    attempt = 0
    while True:
        try:
            return await fn()
        except retry_on:
            attempt += 1
            if attempt > max_attempts:
                raise
            await asyncio.sleep(backoff_s * attempt)
