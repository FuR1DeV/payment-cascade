"""Circuit breaker per provider, shared by all API and worker processes through Redis.

Sliding window of fixed-size buckets: cb:{provider}:b:{bucket} -> hash {total, failures}.
Only technical errors and unknown outcomes count as failures; declines are valid answers.
When the failure rate over the window reaches the threshold (with at least min_requests),
the key cb:{provider}:open is set with a TTL. While it exists, the provider is skipped.
When it expires, traffic resumes with a clean window (a simple half-open state).
If Redis is down the breaker fails open: payments must not stop because of it.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger("payments.circuit_breaker")


class RedisCircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        *,
        window_seconds: int = 60,
        bucket_seconds: int = 10,
        min_requests: int = 10,
        failure_rate: float = 0.5,
        open_seconds: int = 30,
        prefix: str = "cb",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._window = window_seconds
        self._bucket = bucket_seconds
        self._min_requests = min_requests
        self._failure_rate = failure_rate
        self._open_seconds = open_seconds
        self._prefix = prefix
        self._clock = clock

    def _open_key(self, provider: str) -> str:
        return f"{self._prefix}:{provider}:open"

    def _bucket_keys(self, provider: str) -> list[str]:
        current = int(self._clock()) // self._bucket
        count = max(1, self._window // self._bucket)
        return [f"{self._prefix}:{provider}:b:{current - i}" for i in range(count)]

    async def is_available(self, provider: str) -> bool:
        try:
            return not await self._redis.exists(self._open_key(provider))
        except RedisError:
            logger.warning("circuit_breaker_unavailable", exc_info=True)
            return True

    async def record(self, provider: str, *, success: bool) -> None:
        try:
            await self._record(provider, success)
        except RedisError:
            logger.warning("circuit_breaker_unavailable", exc_info=True)

    async def _record(self, provider: str, success: bool) -> None:
        keys = self._bucket_keys(provider)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(keys[0], "total", 1)
            if not success:
                pipe.hincrby(keys[0], "failures", 1)
            pipe.expire(keys[0], self._window + self._bucket)
            await pipe.execute()
        if success:
            return
        total, failures = await self._window_counts(keys)
        if total >= self._min_requests and failures / total >= self._failure_rate:
            opened = await self._redis.set(
                self._open_key(provider), str(int(self._clock())), ex=self._open_seconds, nx=True
            )
            if opened:
                await self._redis.delete(*keys)
                logger.warning(
                    "circuit_opened",
                    extra={
                        "provider": provider,
                        "window_total": total,
                        "window_failures": failures,
                        "open_seconds": self._open_seconds,
                    },
                )

    async def _window_counts(self, keys: list[str]) -> tuple[int, int]:
        async with self._redis.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.hmget(key, "total", "failures")
            rows = await pipe.execute()
        total = sum(int(t or 0) for t, _ in rows)
        failures = sum(int(f or 0) for _, f in rows)
        return total, failures

    async def state(self, provider: str) -> dict[str, Any]:
        try:
            ttl = await self._redis.ttl(self._open_key(provider))
            total, failures = await self._window_counts(self._bucket_keys(provider))
        except RedisError:
            return {"state": "unknown"}
        is_open = ttl is not None and ttl > 0
        return {
            "state": "open" if is_open else "closed",
            "retry_after_seconds": ttl if is_open else None,
            "window_requests": total,
            "window_failures": failures,
        }
