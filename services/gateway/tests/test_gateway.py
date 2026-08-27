import asyncio
import pytest
from app import config
from app.policy import (
    check_auth, validate_chat_request,
    AuthError, ValidationError,
)
from app.resilience import CircuitBreaker, CircuitOpenError, CircuitState, with_retries


# ---- policy: validation ----
def test_validate_caps_max_tokens():
    config.settings.MAX_TOKENS_CAP = 512
    body = validate_chat_request({"messages":[{"role":"user","content":"hi"}], "max_tokens": 99999})
    assert body["max_tokens"] == 512

def test_validate_rejects_empty_messages():
    with pytest.raises(ValidationError):
        validate_chat_request({"messages": []})

def test_validate_rejects_oversized_prompt():
    config.settings.MAX_PROMPT_CHARS = 10
    with pytest.raises(ValidationError):
        validate_chat_request({"messages":[{"role":"user","content":"x"*50}]})
    config.settings.MAX_PROMPT_CHARS = 8000  # reset

# ---- policy: auth ----
def test_auth_disabled_when_no_key():
    config.settings.GATEWAY_API_KEY = ""
    assert check_auth(None) == "anonymous"

def test_auth_rejects_bad_key():
    config.settings.GATEWAY_API_KEY = "secret123"
    with pytest.raises(AuthError):
        check_auth("Bearer wrong")

def test_auth_accepts_good_key():
    config.settings.GATEWAY_API_KEY = "secret123"
    cid = check_auth("Bearer secret123")
    assert cid and cid != "anonymous"
    config.settings.GATEWAY_API_KEY = ""  # reset


# ---- resilience: retries ----
def test_retries_then_succeeds():
    calls = {"n": 0}
    class Transient(Exception): ...
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Transient()
        return "ok"
    out = asyncio.run(with_retries(flaky, max_attempts=3, backoff_s=0, retry_on=(Transient,)))
    assert out == "ok" and calls["n"] == 3

def test_retries_give_up():
    class Transient(Exception): ...
    async def always_fail():
        raise Transient()
    with pytest.raises(Transient):
        asyncio.run(with_retries(always_fail, max_attempts=2, backoff_s=0, retry_on=(Transient,)))


# ---- resilience: circuit breaker ----
def test_circuit_opens_after_threshold():
    async def run():
        cb = CircuitBreaker(fail_threshold=3, reset_timeout_s=10)
        async def boom(): raise ValueError("upstream down")
        for _ in range(3):
            try: await cb.call(boom)
            except ValueError: pass
        assert cb.state == CircuitState.OPEN
        # now it fails fast without calling upstream
        with pytest.raises(CircuitOpenError):
            await cb.call(boom)
    asyncio.run(run())

def test_circuit_recovers_after_timeout():
    async def run():
        cb = CircuitBreaker(fail_threshold=1, reset_timeout_s=0)  # instant cooldown
        async def boom(): raise ValueError()
        try: await cb.call(boom)
        except ValueError: pass
        assert cb.state == CircuitState.OPEN
        # cooldown=0 -> next call is half-open trial; make it succeed
        async def ok(): return "ok"
        out = await cb.call(ok)
        assert out == "ok" and cb.state == CircuitState.CLOSED
    asyncio.run(run())
