# Architecture decisions

Short records of the choices that shape Wakeel, written the way I would for a client.

## 1. LangGraph, with code deciding the order of steps
**Context.** Disputes are a regulated process: approval must never be skipped, and every step must be auditable.

**Decision.** A LangGraph state machine. The model classifies the request and works inside nodes; the routing between nodes is a plain function of the state.

**Consequences.**
- The process is testable and explainable.
- The model cannot "decide" to skip approval.
- Less flexibility for open-ended tasks, which is acceptable here.

## 2. A supervisor with two specialists, not a swarm
**Context.** Policy questions and ledger operations need different tools and permissions.

**Decision.** A supervisor routes to a policy agent (retrieval only) and an operations agent (ledger tools).

**Consequences.**
- Each agent has least privilege and its own prompt.
- Easier to trace and test than peer-to-peer handoffs.

## 3. The model proposes; code verifies; a person approves
**Context.** Models are good at language and judgement, and unreliable at arithmetic and at following limits.

**Decision.**
- Duplicates, the 60-day window, caps and refundable amounts are checked in tools.
- Refunds are proposals until a reviewer approves.
- Refunds carry idempotency keys.

**Consequences.**
- Wrong refunds are prevented by construction, which the evaluations confirm (0 in CI).
- More code to maintain than a prompt.

## 4. Hybrid retrieval, measured rather than assumed
**Context.** Customers use exact terms ("60 days") and paraphrases ("taken twice"), in Arabic and English.

**Decision.** BM25 with Arabic normalisation, plus dense vectors, fused with RRF, plus an optional model reranker.

**Consequences.**
- With the offline embedder, vectors alone scored higher at rank 1 than hybrid (0.70 vs 0.60), and I report that.
- The fusion weights are a tuning decision to make on the client's real question set, not on 30 demo questions.

## 5. Provider-neutral models, with fallback and rules
**Context.**
- Free tiers rate-limit.
- Clients have preferred vendors and data-residency rules.
- An outage must not stop the service.

**Decision.**
- One small interface with adapters for Gemini, OpenAI-compatible APIs (Groq, OpenAI) and Claude.
- Ordered fallback on 429, 5xx and timeouts.
- Deterministic rules when every provider fails.

**Consequences.**
- The vendor is a configuration choice.
- `evals.compare` measures the trade-off per provider.

## 6. SQLite now, Postgres in production
**Context.** Cases awaiting approval must survive restarts.

**Decision.** The LangGraph SQLite checkpointer, plus a case index and a hash-chained audit trail in the same file.

**Consequences.**
- Durable on one instance.
- For several replicas, switch to `langgraph-checkpoint-postgres` and move the audit trail to the bank's database. The interface stays the same.

## 7. Observability by default
**Decision.**
- Every node records a span (latency, provider, model, tokens, tool calls, fallbacks).
- Prometheus metrics come from the spans.
- OpenTelemetry export with GenAI attributes when an endpoint is configured.

**Consequences.** Cost per case, latency, fallback rate and the human override rate can be watched from day one.
