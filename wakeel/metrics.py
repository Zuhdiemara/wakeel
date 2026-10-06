"""Prometheus metrics, derived from each case's trace: what an operations
team watches for an agent in production."""
from prometheus_client import CollectorRegistry, Counter, Histogram

registry = CollectorRegistry()
cases = Counter("wakeel_cases_total", "Cases by final or current status", ["status", "intent"], registry=registry)
decisions = Counter("wakeel_decisions_total", "Human decisions on proposed refunds", ["approved"], registry=registry)
llm_calls = Counter("wakeel_llm_calls_total", "Model calls", ["provider", "model"], registry=registry)
tokens = Counter("wakeel_llm_tokens_total", "Tokens", ["provider", "direction"], registry=registry)
tool_calls = Counter("wakeel_tool_calls_total", "Tool calls by the operations agent", ["tool", "ok"], registry=registry)
fallbacks = Counter("wakeel_fallbacks_total", "Provider failures that moved to the next provider", ["provider"], registry=registry)
node_seconds = Histogram("wakeel_node_seconds", "Time per graph node", ["node"], registry=registry,
                         buckets=(.005, .02, .05, .1, .25, .5, 1, 2, 5, 10, 30))


def observe(spans: list[dict], steps: list[dict] | None = None) -> None:
    for s in spans:
        node_seconds.labels(s["node"]).observe(s.get("ms", 0) / 1000)
        if s.get("provider"):
            llm_calls.labels(s["provider"], s.get("model", "")).inc()
            tokens.labels(s["provider"], "in").inc(s.get("tokens_in", 0))
            tokens.labels(s["provider"], "out").inc(s.get("tokens_out", 0))
    for st in steps or []:
        tool_calls.labels(st["tool"], str("error" not in (st.get("result") or {})).lower()).inc()
