from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wakeel import otel
from wakeel.graph import Agent, Deps
from wakeel.ledger import demo_ledger

from conftest import scripted


def test_each_case_becomes_a_trace_with_genai_attributes(index, monkeypatch):
    monkeypatch.setenv("OTEL_SYNC", "1")
    mem = InMemorySpanExporter()
    tracer = otel.setup(mem)
    plan = [[("propose_refund", {"transaction_id": "tx_1002", "amount_sar": 349, "reason": "duplicate_charge", "policy_section": "disputes#3"})]]
    agent = Agent(Deps(scripted(plan), index, demo_ledger()), on_spans=lambda spans, steps, case: otel.export(tracer, spans, case))
    agent.start("sara", "I was charged twice at Jarir, card 4111 1111 1111 1111")
    spans = mem.get_finished_spans()
    names = [s.name for s in spans]
    assert names[-1] == "case" and {"intake", "supervisor", "policy_agent", "ops_agent"} <= set(names)
    sup = next(s for s in spans if s.name == "supervisor")
    assert sup.attributes["gen_ai.system"] == "scripted" and sup.attributes["gen_ai.usage.input_tokens"] == 50
    root = next(s for s in spans if s.name == "case")
    assert all(s.parent.span_id == root.context.span_id for s in spans if s.name != "case")
    assert "4111" not in str([dict(s.attributes) for s in spans])     # nothing personal leaves
