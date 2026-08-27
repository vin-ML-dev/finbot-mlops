"""The gateway — a policy-and-resilience layer in front of the model.

Request path:
  auth -> rate limit -> validate -> cache check -> [circuit breaker + retries]
  -> call model -> cache store -> return.

It is NOT a second inference engine: it never does the model's work, it governs
and protects access to it. On failure it returns clean errors, never a stack trace."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

from .config import settings
from .policy import (
    PolicyError,
    RedisPolicy,
    check_auth,
    validate_chat_request,
)
from .resilience import CircuitBreaker, CircuitOpenError, with_retries

# --- metrics (scraped by Prometheus via the ServiceMonitor) ---
REQUESTS = Counter(
    "gateway_requests_total", "Gateway requests", ["endpoint", "status"]
)
UPSTREAM_LATENCY = Histogram(
    "gateway_upstream_seconds", "Upstream model call latency (s)"
)
CACHE_HITS = Counter("gateway_cache_hits_total", "Response cache hits")
CB_STATE = Counter("gateway_circuit_events_total", "Circuit breaker events", ["event"])

# transient errors we retry on (timeouts + connection errors, not 4xx)
TRANSIENT = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)

redis_policy = RedisPolicy()
breaker = CircuitBreaker(settings.CB_FAIL_THRESHOLD, settings.CB_RESET_TIMEOUT_S)
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=settings.MODEL_TIMEOUT_S)
    await redis_policy.connect()
    yield
    await redis_policy.close()
    if http_client:
        await http_client.aclose()


app = FastAPI(title="finbot-gateway", lifespan=lifespan)


# ---------------------------------------------------------------------------
# health + metrics
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness — is the process up? (Doesn't depend on the model.)"""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> Response:
    """Readiness — can we serve? Redis is optional (fail-open), so we report
    ready if the process is up; we surface redis state for visibility."""
    redis_ok = await redis_policy.ping()
    return JSONResponse({"status": "ready", "redis": redis_ok})


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# the main proxy endpoint
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    endpoint = "chat_completions"
    try:
        # ---- POLICY: auth ----
        client_id = check_auth(request.headers.get("authorization"))

        # ---- POLICY: rate limit ----
        await redis_policy.enforce_rate_limit(client_id)

        # ---- parse + POLICY: validate ----
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            REQUESTS.labels(endpoint, "422").inc()
            return _err(422, "invalid JSON body")
        body = validate_chat_request(body)

        # ---- POLICY: cache ----
        cached = await redis_policy.cache_get(body)
        if cached is not None:
            CACHE_HITS.inc()
            REQUESTS.labels(endpoint, "200").inc()
            return JSONResponse(cached, headers={"x-cache": "HIT"})

        # ---- RESILIENCE: circuit breaker + bounded retries ----
        async def _call_model() -> dict[str, Any]:
            assert http_client is not None
            with UPSTREAM_LATENCY.time():
                resp = await http_client.post(
                    f"{settings.MODEL_BASE_URL}/v1/chat/completions", json=body
                )
            resp.raise_for_status()
            return resp.json()

        async def _guarded() -> dict[str, Any]:
            return await with_retries(
                _call_model,
                max_attempts=settings.RETRY_MAX_ATTEMPTS,
                backoff_s=settings.RETRY_BACKOFF_S,
                retry_on=TRANSIENT,
            )

        try:
            result = await breaker.call(_guarded)
        except CircuitOpenError:
            CB_STATE.labels("open_rejected").inc()
            REQUESTS.labels(endpoint, "503").inc()
            return _err(503, "model temporarily unavailable (circuit open)")
        except TRANSIENT:
            REQUESTS.labels(endpoint, "504").inc()
            return _err(504, "model timed out")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            REQUESTS.labels(endpoint, str(code)).inc()
            return _err(502, f"model returned {code}")

        # ---- POLICY: cache store ----
        await redis_policy.cache_set(body, result)
        REQUESTS.labels(endpoint, "200").inc()
        return JSONResponse(result, headers={"x-cache": "MISS"})

    except PolicyError as e:
        REQUESTS.labels(endpoint, str(e.status_code)).inc()
        return _err(e.status_code, e.detail)
    except Exception:
        # never leak a stack trace — clean error
        REQUESTS.labels(endpoint, "500").inc()
        return _err(500, "internal error")


def _err(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"error": {"message": detail, "code": status}}, status_code=status)
