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


injection_flags = Counter("wakeel_injection_flags_total", "Messages the injection screens sent to a person", registry=registry)
cross_checks = Counter("wakeel_cross_checks_total", "Cases where the rules raised a refund the model missed", registry=registry)
rules_fallback = Counter("wakeel_rules_fallback_total", "Steps finished by rules because no model answered", ["node"], registry=registry)
llm_errors = Counter("wakeel_llm_errors_total", "Model calls that failed (rate limits, outages, rejected requests)", registry=registry)


def observe(spans: list[dict], steps: list[dict] | None = None) -> None:
    for s in spans:
        if s["node"] == "intake" and s.get("injection"):
            injection_flags.inc()
        if s.get("cross_check"):
            cross_checks.inc()
        if s.get("mode") == "rules" or s.get("why") == "rules":
            rules_fallback.labels(s["node"]).inc()
        if s.get("llm_errors"):
            llm_errors.inc(len(s["llm_errors"]))
        node_seconds.labels(s["node"]).observe(s.get("ms", 0) / 1000)
        if s.get("provider"):
            llm_calls.labels(s["provider"], s.get("model", "")).inc()
            tokens.labels(s["provider"], "in").inc(s.get("tokens_in", 0))
            tokens.labels(s["provider"], "out").inc(s.get("tokens_out", 0))
    for st in steps or []:
        tool_calls.labels(st["tool"], str("error" not in (st.get("result") or {})).lower()).inc()

from prometheus_client import Gauge

jobs = Gauge("wakeel_jobs", "Queued work by state", ["state"], registry=registry)
jobs_done = Counter("wakeel_jobs_finished_total", "Jobs finished, retried or dead", ["state"], registry=registry)


def jobs_state(counts: dict) -> None:
    for s in ("queued", "running", "done", "dead"):
        jobs.labels(s).set(counts.get(s, 0))
