"""Configuration — everything comes from environment variables so the same image
runs anywhere. Secrets (the API key) come from the sealed Kubernetes Secret;
the rest have sane defaults."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    # --- upstream model (the llama.cpp service from component 5) ---
    MODEL_BASE_URL: str = os.getenv("MODEL_BASE_URL", "http://llama-cpp-svc:8080")
    MODEL_TIMEOUT_S: float = _float("MODEL_TIMEOUT_S", 60.0)

    # --- redis (rate-limit counters + response cache, from component 3) ---
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis-svc:6379/0")

    # --- policy: auth ---
    # The gateway API key clients must present. Injected from the sealed
    # gateway-secret (key GATEWAY_API_KEY). If empty, auth is disabled (dev only).
    GATEWAY_API_KEY: str = os.getenv("GATEWAY_API_KEY", "")

    # --- policy: request validation ---
    MAX_PROMPT_CHARS: int = _int("MAX_PROMPT_CHARS", 8000)
    MAX_TOKENS_CAP: int = _int("MAX_TOKENS_CAP", 512)

    # --- policy: rate limiting (per client key, sliding window) ---
    RATE_LIMIT_REQUESTS: int = _int("RATE_LIMIT_REQUESTS", 30)
    RATE_LIMIT_WINDOW_S: int = _int("RATE_LIMIT_WINDOW_S", 60)

    # --- policy: response cache ---
    CACHE_ENABLED: bool = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    CACHE_TTL_S: int = _int("CACHE_TTL_S", 300)

    # --- resilience: retries (bounded, transient-only) ---
    RETRY_MAX_ATTEMPTS: int = _int("RETRY_MAX_ATTEMPTS", 2)
    RETRY_BACKOFF_S: float = _float("RETRY_BACKOFF_S", 0.5)

    # --- resilience: circuit breaker ---
    CB_FAIL_THRESHOLD: int = _int("CB_FAIL_THRESHOLD", 5)      # consecutive fails to open
    CB_RESET_TIMEOUT_S: float = _float("CB_RESET_TIMEOUT_S", 20.0)  # how long to stay open


settings = Settings()
