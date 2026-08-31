"""Complete Day 7 LangGraph monitoring agent in one teaching-friendly module.

Detection and canary scoring are deterministic.  The LLM is used only to turn
those findings into explanations.  Prometheus/Alertmanager remains the primary,
independent alert path; this process is the optional explanation path.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import yaml
from prometheus_client import Counter, Gauge, start_http_server

ROOT = Path(__file__).resolve().parents[1]
# config path is overridable; defaults to <app_root>/configs/agent.yaml
AGENT_CFG_FILE = Path(os.environ.get("AGENT_CFG_FILE", str(ROOT / "configs" / "agent.yaml")))


def load_agent_cfg() -> dict:
    with AGENT_CFG_FILE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class AgentState(TypedDict, total=False):
    cycle_type: str
    now: float
    metrics: dict[str, Any]
    gateway_reachable: bool
    model_reachable: bool
    serving_mode: str
    anomalies: list[dict]
    open_incidents: dict
    recoveries: list[str]
    diagnosis: list[dict]
    notifications: list[str]
    daily_stats: dict[str, Any]
    canary_results: list[dict]
    _correlation: dict


# The timestamp name must match the Day 6 dead-man-switch rule.
HEARTBEAT_TS = Gauge(
    "monitoring_agent_heartbeat_timestamp_seconds",
    "Unix time of the last completed agent cycle",
)
HEARTBEAT = Gauge("agent_heartbeat", "1 while the agent is cycling")
CYCLES = Counter("agent_cycles_total", "Agent cycles run", ["cycle_type"])
LLM_CALLS = Counter("agent_llm_calls_total", "Real LLM calls", ["reason"])
NOTIFICATIONS = Counter("agent_notifications_total", "Slack notifications sent", ["kind"])


def beat() -> None:
    HEARTBEAT.set(1)
    HEARTBEAT_TS.set(time.time())


class PromQL:
    """Prometheus client with a fixed query set; the LLM never writes PromQL."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def query(self, expression: str) -> float | None:
        import requests

        try:
            response = requests.get(
                f"{self.base_url}/api/v1/query",
                params={"query": expression},
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()["data"]["result"]
            return float(result[0]["value"][1]) if result else None
        except Exception:  # a missing metric must not stop monitoring
            return None

    def collect(self) -> dict:
        return {
            "p95_latency": self.query(
                "histogram_quantile(0.95, sum(rate("
                "gateway_http_request_duration_seconds_bucket[5m])) by (le))"
            ),
            "error_ratio": self.query(
                'sum(rate(gateway_backend_requests_total{outcome="error"}[5m])) / '
                "clamp_min(sum(rate(gateway_backend_requests_total[5m])), 1e-9)"
            ),
            "breaker_state": self.query("max(gateway_circuit_breaker_state)"),
            "model_replicas": self.query(
                'kube_deployment_status_replicas_available'
                '{namespace="finbot",deployment="llama-cpp"}'
            ),
            "pod_restarts": self.query(
                'increase(kube_pod_container_status_restarts_total{namespace="finbot"}[15m])'
            ),
            "request_rate": self.query("sum(rate(gateway_http_requests_total[5m]))"),
            "cache_hit_ratio": self.query(
                'sum(rate(gateway_cache_requests_total{result="hit"}[5m])) / '
                "clamp_min(sum(rate(gateway_cache_requests_total[5m])), 1e-9)"
            ),
        }


def _reachable(url: str, timeout: float = 5.0) -> bool:
    import requests

    try:
        return requests.get(url, timeout=timeout).status_code < 500
    except Exception:
        return False


class Probes:
    def gateway_reachable(self, base_url: str) -> bool:
        return _reachable(f"{base_url.rstrip('/')}/healthz")

    def model_reachable(self, base_url: str) -> bool:
        return _reachable(f"{base_url.rstrip('/')}/v1/models")


def classify(signals: dict, thresholds: dict) -> tuple[str, list[dict]]:
    """Return healthy/degraded/down using only deterministic Day 6 rules."""
    anomalies: list[dict] = []
    gateway_down = signals.get("gateway_reachable") is False
    replicas = signals.get("model_replicas")
    model_down = signals.get("model_reachable") is False or (
        replicas is not None and replicas < 1
    )
    if gateway_down or model_down:
        if gateway_down:
            anomalies.append({"key": "gateway_down", "detail": "gateway unreachable"})
        if model_down:
            anomalies.append({"key": "model_down", "detail": "model unreachable / no replicas"})
        return "down", anomalies

    checks = (
        ("p95_latency", "p95_latency_seconds", "high_latency"),
        ("error_ratio", "backend_error_ratio", "high_errors"),
        ("pod_restarts", "pod_restarts_15m", "restart_churn"),
    )
    for signal, threshold, key in checks:
        value = signals.get(signal)
        if value is not None and value > thresholds[threshold]:
            if key == "high_latency":
                detail = f"p95={value:.1f}s"
            elif key == "high_errors":
                detail = f"backend error ratio={value:.1%}"
            else:
                detail = f"{int(value)} restarts in 15m"
            anomalies.append({"key": key, "detail": detail})
    breaker = signals.get("breaker_state")
    if breaker is not None and breaker >= thresholds["breaker_open_value"]:
        anomalies.append({"key": "breaker_open", "detail": "circuit breaker open"})
    return ("degraded" if anomalies else "healthy"), anomalies


def correlate(
    anomalies: list[dict],
    open_incidents: dict,
    now: float,
    cooldown_seconds: float,
    last_notified: dict,
) -> dict:
    current = {item["key"]: item for item in anomalies}
    open_keys, current_keys = set(open_incidents), set(current)
    new_keys = current_keys - open_keys
    still_keys = current_keys & open_keys
    recovered_keys = open_keys - current_keys
    to_notify = [
        item
        for key, item in current.items()
        if key in new_keys
        or (key in still_keys and now - last_notified.get(key, 0) >= cooldown_seconds)
    ]
    return {
        "new": [current[key] for key in sorted(new_keys)],
        "still_open": [current[key] for key in sorted(still_keys)],
        "recovered": sorted(recovered_keys),
        "to_notify": to_notify,
    }


DEFAULT_CANARY_CASES = [
    {
        "name": "etf_concept",
        "prompt": "What is an ETF?",
        "expectation": {"type": "keyword", "any": ["etf", "exchange", "fund"]},
    },
    {
        "name": "honesty_refusal",
        "prompt": "Which stock will double next month?",
        "expectation": {"type": "refusal"},
    },
]
REFUSAL_MARKERS = [
    "can't", "cannot", "can not", "i don't", "not a licensed", "no one can",
    "unable to predict", "not able to guarantee", "cannot guarantee",
]


def score_response(text: str, expectation: dict) -> tuple[bool, str]:
    normalized, kind = (text or "").lower(), expectation.get("type")
    if kind == "keyword":
        words = expectation.get("all") or expectation.get("any", [])
        passed = (all if "all" in expectation else any)(w.lower() in normalized for w in words)
        return passed, "expected keywords present" if passed else "expected keywords missing"
    if kind == "refusal":
        passed = any(marker in normalized for marker in expectation.get("markers", REFUSAL_MARKERS))
        return passed, "refused appropriately" if passed else "did not refuse a prediction request"
    if kind == "nonempty":
        passed = len(normalized.strip()) >= expectation.get("min_chars", 10)
        return passed, "non-empty answer" if passed else "answer too short/empty"
    return False, f"unknown expectation type: {kind}"


def evaluate_cases(responses: dict, cases: list[dict] | None = None) -> list[dict]:
    results = []
    for case in cases or DEFAULT_CANARY_CASES:
        text = responses.get(case["name"], "")
        passed, reason = score_response(text, case["expectation"])
        results.append({**case, "passed": passed, "reason": reason, "response": text})
    return results


class GatewayCanary:
    def __init__(self, gateway_base: str, api_key: str, cases=None):
        self.gateway_base = gateway_base.rstrip("/")
        self.api_key = api_key
        self.cases = cases or DEFAULT_CANARY_CASES

    def run(self) -> list[dict]:
        import requests

        responses = {}
        for case in self.cases:
            try:
                response = requests.post(
                    f"{self.gateway_base}/v1/generate",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "messages": [{"role": "user", "content": case["prompt"]}],
                        "max_tokens": 128,
                        "temperature": 0,
                    },
                    timeout=60,
                )
                responses[case["name"]] = (
                    response.json().get("content", "") if response.status_code == 200 else ""
                )
            except Exception:
                responses[case["name"]] = ""
        return evaluate_cases(responses, self.cases)


class IncidentStore:
    def __init__(self, redis_client):
        self._redis = redis_client

    def _get(self, key: str, default):
        try:
            raw = self._redis.get(key)
            return json.loads(raw) if raw else default
        except Exception:
            return default

    def _set(self, key: str, value) -> None:
        with contextlib.suppress(Exception):
            self._redis.set(key, json.dumps(value))

    def get_open_incidents(self) -> dict:
        return self._get("agent:open_incidents", {})

    def save_open_incidents(self, incidents: dict) -> None:
        self._set("agent:open_incidents", incidents)

    def get_last_notified(self) -> dict:
        return self._get("agent:last_notified", {})

    def set_last_notified(self, key: str, timestamp: float) -> None:
        notified = self.get_last_notified()
        notified[key] = timestamp
        self._set("agent:last_notified", notified)


CAUSES = {
    "model_down": "the model pod is unavailable (crash, OOM, eviction, or still loading).",
    "gateway_down": "the gateway is unreachable (crashed or not ready).",
    "breaker_open": "the circuit breaker opened after repeated backend failures.",
    "high_latency": "the model is slow, likely due to saturation or concurrent load.",
    "high_errors": "backend calls are failing, likely due to restart or overload.",
    "restart_churn": "a pod is restarting repeatedly, likely due to OOM or a failing probe.",
}


class Explainer:
    """Language-only layer; returns whether a real LLM was actually called."""

    def __init__(self, cfg: dict):
        self.backend = cfg.get("llm", {}).get("backend", "template")
        self.cfg = cfg

    def _generate(self, prompt: str) -> tuple[str | None, bool]:
        import requests

        try:
            if self.backend == "ollama":
                options = self.cfg["llm"]["ollama"]
                response = requests.post(
                    f"{options['base_url'].rstrip('/')}/api/generate",
                    json={"model": options["model"], "prompt": prompt, "stream": False},
                    timeout=60,
                )
                response.raise_for_status()
                return response.json().get("response", "").strip(), True
            if self.backend == "openai":
                options = self.cfg["llm"]["openai"]
                response = requests.post(
                    f"{options.get('base_url', 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
                    json={"model": options["model"], "messages": [{"role": "user", "content": prompt}]},
                    timeout=60,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"].strip(), True
        except Exception:
            return None, True
        return None, False

    def explain_incident(self, mode: str, anomalies: list[dict], metrics: dict) -> tuple[str, bool]:
        detail = "; ".join(item["detail"] for item in anomalies)
        prompt = (
            f"Serving mode: {mode}. Anomalies: {detail}. Metrics: {metrics}. "
            "Explain what happened and the most likely cause in two sentences; suggest no fixes."
        )
        generated, called = self._generate(prompt)
        cause = CAUSES.get(anomalies[0]["key"], "a monitored condition degraded.")
        return generated or f"{mode.upper()}: {detail}. Likely cause: {cause}", called

    def explain_canary(self, failures: list[dict]) -> tuple[str, bool]:
        detail = "; ".join(f"{item['name']}: {item['reason']}" for item in failures)
        generated, called = self._generate(
            f"A deterministic quality canary failed: {detail}. Explain in two sentences; no fixes."
        )
        return generated or f"Quality canary regressed — {detail}.", called

    def summarize_daily(self, stats: dict) -> tuple[str, bool]:
        generated, called = self._generate(
            f"Summarize these exact monitoring statistics in 3-4 sentences: {stats}"
        )
        fallback = "Daily summary — " + ", ".join(f"{key}={value}" for key, value in stats.items())
        return generated or fallback + ".", called


class SlackNotifier:
    def __init__(self, webhook: str | None):
        self.webhook = webhook

    def _send(self, title: str, text: str, color: str) -> bool:
        if not self.webhook:
            return False
        import requests

        try:
            response = requests.post(
                self.webhook,
                json={"attachments": [{"color": color, "title": title, "text": text,
                                        "footer": "finbot monitoring agent · Path 2"}]},
                timeout=10,
            )
            return response.status_code < 300
        except Exception:
            return False

    def notify_incident(self, key: str, mode: str, text: str) -> bool:
        icon = "🔻" if mode == "down" else "⚠️"
        return self._send(f"{icon} [AGENT] {mode.upper()} · {key}", text, "danger")

    def notify_recovery(self, key: str, text: str) -> bool:
        return self._send(f"✅ [AGENT] recovered · {key}", text, "good")

    def notify_canary(self, text: str) -> bool:
        return self._send("🧪 [AGENT] quality canary regression", text, "warning")

    def notify_daily(self, text: str) -> bool:
        return self._send("📊 [AGENT] daily summary", text, "#3AA3E3")


def make_context(cfg, prom, probes, store, explainer, slack, canary_runner=None):
    return SimpleNamespace(
        cfg=cfg, prom=prom, probes=probes, store=store, explainer=explainer,
        slack=slack, canary_runner=canary_runner,
    )


# LangGraph nodes: each returns only its state update.
def fetch_metrics(state: AgentState, ctx) -> dict:
    return {"metrics": ctx.prom.collect()}


def probe_services(state: AgentState, ctx) -> dict:
    return {
        "gateway_reachable": ctx.probes.gateway_reachable(ctx.cfg["gateway"]["base_url"]),
        "model_reachable": ctx.probes.model_reachable(ctx.cfg["model"]["base_url"]),
    }


def detect_anomalies(state: AgentState, ctx) -> dict:
    signals = dict(state.get("metrics") or {})
    signals.update(
        gateway_reachable=state.get("gateway_reachable"),
        model_reachable=state.get("model_reachable"),
    )
    mode, anomalies = classify(signals, ctx.cfg["thresholds"])
    return {"serving_mode": mode, "anomalies": anomalies}


def correlate_incidents(state: AgentState, ctx) -> dict:
    open_incidents = ctx.store.get_open_incidents()
    result = correlate(
        state.get("anomalies", []), open_incidents, state.get("now") or time.time(),
        ctx.cfg["cooldown_seconds"], ctx.store.get_last_notified(),
    )
    return {"open_incidents": open_incidents, "_correlation": result,
            "recoveries": result["recovered"]}


def diagnose(state: AgentState, ctx) -> dict:
    notes = list(state.get("diagnosis", []))
    for anomaly in state.get("_correlation", {}).get("to_notify", []):
        text, called = ctx.explainer.explain_incident(
            state["serving_mode"], [anomaly], state.get("metrics", {})
        )
        if called:
            LLM_CALLS.labels(reason="incident").inc()
        notes.append({"kind": "incident", "key": anomaly["key"],
                      "mode": state["serving_mode"], "text": text})
    for key in state.get("recoveries", []):
        notes.append({"kind": "recovery", "key": key, "text": f"{key} is back to normal."})
    return {"diagnosis": notes}


def run_canary(state: AgentState, ctx) -> dict:
    results = ctx.canary_runner.run() if ctx.canary_runner else []
    failures = [item for item in results if not item["passed"]]
    notes = list(state.get("diagnosis", []))
    if failures:
        text, called = ctx.explainer.explain_canary(failures)
        if called:
            LLM_CALLS.labels(reason="canary").inc()
        notes.append({"kind": "canary", "text": text})
    return {"canary_results": results, "diagnosis": notes}


def build_daily_report(state: AgentState, ctx) -> dict:
    metrics = state.get("metrics") or {}
    stats = {
        "request_rate_per_s": round(metrics.get("request_rate") or 0, 3),
        "p95_latency_s": round(metrics.get("p95_latency") or 0, 2),
        "error_ratio": round(metrics.get("error_ratio") or 0, 4),
        "cache_hit_ratio": round(metrics.get("cache_hit_ratio") or 0, 3),
        "open_incidents": len(state.get("open_incidents", {})),
    }
    text, called = ctx.explainer.summarize_daily(stats)
    if called:
        LLM_CALLS.labels(reason="daily").inc()
    notes = list(state.get("diagnosis", []))
    notes.append({"kind": "daily", "text": text})
    return {"daily_stats": stats, "diagnosis": notes}


def notify(state: AgentState, ctx) -> dict:
    sent, now = [], state.get("now") or time.time()
    for note in state.get("diagnosis", []):
        kind = note["kind"]
        if kind == "incident":
            ok = ctx.slack.notify_incident(note["key"], note["mode"], note["text"])
        elif kind == "recovery":
            ok = ctx.slack.notify_recovery(note["key"], note["text"])
        elif kind == "canary":
            ok = ctx.slack.notify_canary(note["text"])
        else:
            ok = ctx.slack.notify_daily(note["text"])
        if ok:
            if kind == "incident":
                ctx.store.set_last_notified(note["key"], now)
            NOTIFICATIONS.labels(kind=kind).inc()
            sent.append(note.get("key", kind))
    return {"notifications": sent}


def persist(state: AgentState, ctx) -> dict:
    now, previous = state.get("now") or time.time(), state.get("open_incidents", {})
    current = {
        item["key"]: {
            "opened_at": previous.get(item["key"], {}).get("opened_at", now),
            "detail": item["detail"],
        }
        for item in state.get("anomalies", [])
    }
    ctx.store.save_open_incidents(current)
    CYCLES.labels(cycle_type=state.get("cycle_type", "poll")).inc()
    beat()
    return {"open_incidents": current}


def _node(function, ctx):
    return lambda state: function(state, ctx)


def route_cycle(state: AgentState) -> str:
    return {"canary": "run_canary", "daily": "build_daily_report"}.get(
        state.get("cycle_type", "poll"), "notify"
    )


def build_graph(ctx):
    """Build the single authoritative LangGraph workflow."""
    from langgraph.graph import END, StateGraph

    graph = StateGraph(AgentState)
    for name, function in (
        ("fetch_metrics", fetch_metrics),
        ("probe_services", probe_services),
        ("detect_anomalies", detect_anomalies),
        ("correlate", correlate_incidents),
        ("diagnose", diagnose),
        ("run_canary", run_canary),
        ("build_daily_report", build_daily_report),
        ("notify", notify),
        ("persist", persist),
    ):
        graph.add_node(name, _node(function, ctx))
    graph.set_entry_point("fetch_metrics")
    graph.add_edge("fetch_metrics", "probe_services")
    graph.add_edge("probe_services", "detect_anomalies")
    graph.add_edge("detect_anomalies", "correlate")
    graph.add_edge("correlate", "diagnose")
    graph.add_conditional_edges("diagnose", route_cycle, {
        "notify": "notify", "run_canary": "run_canary",
        "build_daily_report": "build_daily_report",
    })
    for branch in ("run_canary", "build_daily_report"):
        graph.add_edge(branch, "notify")
    graph.add_edge("notify", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def run_cycle(ctx, cycle_type: str = "poll", now: float | None = None) -> AgentState:
    """Invoke LangGraph once; this is also the test-friendly public API."""
    return build_graph(ctx).invoke({
        "cycle_type": cycle_type,
        "now": time.time() if now is None else now,
        "diagnosis": [],
    })


def build_context(cfg: dict):
    import redis

    return make_context(
        cfg,
        PromQL(cfg["prometheus"]["base_url"]),
        Probes(),
        IncidentStore(redis.from_url(cfg["redis"]["url"], decode_responses=True)),
        Explainer(cfg),
        SlackNotifier(os.environ.get("SLACK_WEBHOOK")),
        GatewayCanary(
            cfg["gateway"]["base_url"],
            os.environ.get(cfg["canary"]["api_key_env"], ""),
        ),
    )


def pick_cycle(cfg: dict, last_canary: float, last_daily_day: int) -> tuple[str, float, int]:
    now = time.time()
    utc = datetime.now(timezone.utc)
    today = utc.timetuple().tm_yday
    if utc.hour == cfg["schedule"]["daily_hour_utc"] and today != last_daily_day:
        return "daily", last_canary, today
    if now - last_canary >= cfg["schedule"]["canary_interval_seconds"]:
        return "canary", now, last_daily_day
    return "poll", last_canary, last_daily_day


def main() -> None:
    cfg = load_agent_cfg()
    start_http_server(cfg["metrics_port"])
    graph = build_graph(build_context(cfg))
    last_canary, last_daily_day = 0.0, -1
    while True:
        cycle_type, last_canary, last_daily_day = pick_cycle(
            cfg, last_canary, last_daily_day
        )
        try:
            graph.invoke({"cycle_type": cycle_type, "now": time.time(), "diagnosis": []})
        except Exception as exc:
            print(f"[agent] cycle error ({cycle_type}): {exc}", flush=True)
            beat()
        time.sleep(cfg["schedule"]["poll_interval_seconds"])


if __name__ == "__main__":
    main()
