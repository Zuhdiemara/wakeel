"""OpenTelemetry export of each case's trace.

Set OTEL_EXPORTER_OTLP_ENDPOINT (Langfuse, Grafana Tempo, Jaeger, Datadog …)
and every case becomes a trace: one root span per case and one child span per
graph step, with model attributes named after the OpenTelemetry GenAI
semantic conventions (gen_ai.system, gen_ai.request.model, gen_ai.usage.*).
Only the masked text and ids are exported, never message bodies.
"""
from __future__ import annotations

import json
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

_tracer = None


def setup(exporter: SpanExporter | None = None):
    """Returns a tracer, or None when no endpoint or exporter is configured."""
    global _tracer
    if exporter is None and not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "wakeel"}))
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    provider.add_span_processor(SimpleSpanProcessor(exporter) if os.getenv("OTEL_SYNC") else BatchSpanProcessor(exporter))
    _tracer = provider.get_tracer("wakeel")
    return _tracer


def export(tracer, spans: list[dict], case: dict) -> None:
    if tracer is None or not spans:
        return
    ns = lambda sec: int(sec * 1e9)
    start = min(s["start"] for s in spans)
    end = max(s["start"] + s["ms"] / 1000 for s in spans)
    root = tracer.start_span("case", start_time=ns(start), attributes={
        "wakeel.case_id": case["case_id"], "wakeel.intent": case.get("intent", ""), "wakeel.status": case.get("status", ""),
        "wakeel.lang": case.get("lang", "")})
    ctx = trace.set_span_in_context(root)
    for s in spans:
        attrs = {"wakeel.node": s["node"]}
        if s.get("provider"):
            attrs.update({"gen_ai.system": s["provider"], "gen_ai.request.model": s.get("model", ""),
                          "gen_ai.usage.input_tokens": s.get("tokens_in", 0), "gen_ai.usage.output_tokens": s.get("tokens_out", 0)})
        for k, v in s.items():
            if k not in ("node", "ms", "start", "provider", "model", "tokens_in", "tokens_out"):
                attrs[f"wakeel.{k}"] = v if isinstance(v, (str, bool, int, float)) else json.dumps(v, ensure_ascii=False, default=str)
        sp = tracer.start_span(s["node"], context=ctx, start_time=ns(s["start"]), attributes=attrs)
        sp.end(end_time=ns(s["start"] + s["ms"] / 1000))
    root.end(end_time=ns(end))
