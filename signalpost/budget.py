"""Global run budget: outbound requests (with named reservations), wall-clock deadline and declared cost."""
from __future__ import annotations

import threading
import time
from collections import Counter


class BudgetExhausted(Exception):
    pass


class Budget:
    def __init__(self, max_requests: int, time_budget_s: float, reserve_s: float = 0.0):
        self.max_requests = max_requests
        self.started = time.monotonic()
        self.deadline = self.started + time_budget_s
        # Stop starting new optional work once this point is reached, leaving time to finalize.
        self.soft_deadline = self.deadline - reserve_s
        self.requests = 0
        self.pools: dict[str, int] = {}
        self.bytes = 0
        self.by_class: Counter[str] = Counter()
        self.cost_usd = 0.0
        self._lock = threading.Lock()

    @property
    def reserved(self) -> int:
        return sum(self.pools.values())

    # Requests -----------------------------------------------------------------
    def remaining(self) -> int:
        with self._lock:
            return self.max_requests - self.requests - self.reserved

    def reserve(self, count: int, pool: str = "default") -> bool:
        """Set aside requests for a phase so other work cannot consume them."""
        with self._lock:
            if count <= 0 or self.requests + self.reserved + count > self.max_requests:
                return False
            self.pools[pool] = self.pools.get(pool, 0) + count
            return True

    def release(self, pool: str = "default") -> None:
        with self._lock:
            self.pools.pop(pool, None)

    def take(self, source_class: str, reserved: bool | str = False) -> None:
        pool = "default" if reserved is True else (reserved or None)
        with self._lock:
            if pool and self.pools.get(pool, 0) > 0:
                self.pools[pool] -= 1
            elif self.requests + self.reserved >= self.max_requests:
                raise BudgetExhausted("outbound request budget exhausted")
            self.requests += 1
            self.by_class[source_class] += 1

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self.bytes += count

    # Time ---------------------------------------------------------------------
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def past_soft_deadline(self) -> bool:
        return time.monotonic() >= self.soft_deadline

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "max_requests": self.max_requests,
                "requests": self.requests,
                "requests_by_source_class": dict(self.by_class),
                "bytes": self.bytes,
                "elapsed_s": round(self.elapsed(), 1),
                "third_party_cost_usd": round(self.cost_usd, 4),
            }
