"""Policy layer — enforces the rules. Auth, request validation, rate limiting,
and response caching. Rate-limit counters and the cache live in Redis (shared
across all gateway replicas), so scaling out doesn't reset limits or caches."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

import redis.asyncio as redis

from .config import settings


class PolicyError(Exception):
    """Base for policy rejections; carries an HTTP status + message."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class AuthError(PolicyError):
    def __init__(self, detail: str = "invalid or missing API key") -> None:
        super().__init__(401, detail)


class ValidationError(PolicyError):
    def __init__(self, detail: str) -> None:
        super().__init__(422, detail)


class RateLimitError(PolicyError):
    def __init__(self, detail: str = "rate limit exceeded") -> None:
        super().__init__(429, detail)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def check_auth(authorization: Optional[str]) -> str:
    """Validate the Bearer key. Returns a client id (the key) for rate-limiting.
    If no key is configured, auth is disabled (dev only) and all clients share
    the id 'anonymous'."""
    if not settings.GATEWAY_API_KEY:
        return "anonymous"
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError()
    token = authorization.split(" ", 1)[1].strip()
    # constant-time-ish compare
    if not _consteq(token, settings.GATEWAY_API_KEY):
        raise AuthError()
    # client id = short hash of the key (don't log raw keys)
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _consteq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a.encode(), b.encode()):
        result |= x ^ y
    return result == 0


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------
def validate_chat_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize an OpenAI-style chat request. Caps max_tokens and
    rejects oversized prompts (policy limits, not the model's)."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValidationError("`messages` must be a non-empty list")

    total_chars = 0
    for m in messages:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            raise ValidationError("each message needs `role` and `content`")
        if not isinstance(m["content"], str):
            raise ValidationError("message `content` must be a string")
        total_chars += len(m["content"])

    if total_chars > settings.MAX_PROMPT_CHARS:
        raise ValidationError(
            f"prompt too long: {total_chars} > {settings.MAX_PROMPT_CHARS} chars"
        )

    # cap max_tokens to protect the model from huge generations
    requested = body.get("max_tokens", settings.MAX_TOKENS_CAP)
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        raise ValidationError("`max_tokens` must be an integer")
    body["max_tokens"] = max(1, min(requested, settings.MAX_TOKENS_CAP))

    return body


# ---------------------------------------------------------------------------
# rate limiting + caching (Redis-backed)
# ---------------------------------------------------------------------------
class RedisPolicy:
    """Rate limiting (fixed-window counter) and response caching in Redis.
    Fail-open on Redis errors: if Redis is down, we DON'T block inference — a
    cache/limit outage must not take the model offline."""

    def __init__(self) -> None:
        self._r: Optional[redis.Redis] = None

    async def connect(self) -> None:
        self._r = redis.from_url(settings.REDIS_URL, decode_responses=True)

    async def close(self) -> None:
        if self._r is not None:
            await self._r.aclose()

    async def ping(self) -> bool:
        try:
            return bool(self._r) and await self._r.ping()
        except Exception:
            return False

    async def enforce_rate_limit(self, client_id: str) -> None:
        """Fixed-window counter. window key rolls every RATE_LIMIT_WINDOW_S."""
        if self._r is None:
            return
        window = int(time.time()) // settings.RATE_LIMIT_WINDOW_S
        key = f"rl:{client_id}:{window}"
        try:
            count = await self._r.incr(key)
            if count == 1:
                await self._r.expire(key, settings.RATE_LIMIT_WINDOW_S)
        except Exception:
            return  # fail-open: Redis trouble must not block inference
        if count > settings.RATE_LIMIT_REQUESTS:
            raise RateLimitError(
                f"rate limit: {settings.RATE_LIMIT_REQUESTS}/"
                f"{settings.RATE_LIMIT_WINDOW_S}s exceeded"
            )

    @staticmethod
    def _cache_key(body: dict[str, Any]) -> str:
        payload = json.dumps(body, sort_keys=True)
        return "cache:" + hashlib.sha256(payload.encode()).hexdigest()

    async def cache_get(self, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not settings.CACHE_ENABLED or self._r is None:
            return None
        try:
            raw = await self._r.get(self._cache_key(body))
            return json.loads(raw) if raw else None
        except Exception:
            return None  # fail-open

    async def cache_set(self, body: dict[str, Any], response: dict[str, Any]) -> None:
        if not settings.CACHE_ENABLED or self._r is None:
            return
        try:
            await self._r.set(
                self._cache_key(body), json.dumps(response), ex=settings.CACHE_TTL_S
            )
        except Exception:
            return  # fail-open
