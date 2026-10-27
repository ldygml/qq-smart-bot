from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = max(0, limit)
        self.window_seconds = max(1.0, window_seconds)
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        if self.limit == 0:
            return True
        current = time.monotonic() if now is None else now
        bucket = self._events[key]
        cutoff = current - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        return True

