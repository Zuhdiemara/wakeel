# Wakeel · وكيل

**A card-dispute agent for a Saudi bank**, built the way a regulated client would need it:
- **Workflow:** LangGraph orchestration with a supervisor and two specialist agents.
- **Knowledge:** hybrid RAG over the bank's policies in Arabic and English.
- **Tools:** tool calling against a real double-entry ledger.
- **Control:** a human approves every refund.
- **Safety:** PII masking, prompt-injection defences.
- **Proof:** an evaluation suite that fails the build on any unsafe outcome.
- **Interoperability:** the same tools exposed over MCP.

> Sahm Bank, its customers and its policies are **fictional**. No real money moves.

```
customer: "I was charged twice at Jarir Bookstore, card 4111 1111 1111 1111"
  intake        masks the card → [card ••1111], language en, injection screen clear
  supervisor    intent = duplicate_charge
  policy_agent  hybrid search → disputes#3 "Duplicate charges", cited
  ops_agent     find_duplicates → tx_1001 (original), tx_1002 (duplicate)
                propose_refund(tx_1002, 349.00 SAR, disputes#3)   ← proposal only
  approval      ⏸ interrupt: a bank employee approves
  execute       refund with idempotency key wakeel:<case>:tx_1002
  respond       "Your refund of 349.00 SAR … within 3 working days [disputes#3]"
```

## Architecture

The exact graph, generated from the compiled code: [docs/graph.md](docs/graph.md).

```mermaid
flowchart LR
  C[Customer, Arabic or English] --> I[intake<br/>PII masking · language · injection screen]
  I --> S{supervisor<br/>model classifies,<br/>code routes}
  S --> P[policy_agent<br/>hybrid RAG + rerank,<br/>citations checked]
  S --> O[ops_agent<br/>ReAct tool loop]
  S --> A[[approval<br/>LangGraph interrupt]]
  S --> E[execute<br/>idempotent refund]
  S --> H[handoff<br/>fraud · injection · out of scope]
  S --> R[respond<br/>numbers checked against facts]
  P --> S
  O --> S
  A --> S
  E --> S
  O <--> T[(tools: search_policy · list_transactions<br/>find_duplicates · propose_refund)]
  E --> L[(Ledger: in-memory demo<br/>or Daftar, exactly-once)]
  T --> L
  M[MCP clients] <--> T
```

| Concern | How Wakeel handles it |
|---|---|
| **Orchestration** | A LangGraph `StateGraph` with a checkpointer. The model classifies the request, but **code decides the order of steps**, so approval can never be skipped. A human approval is a LangGraph `interrupt`, resumed with `Command(resume=…)`. |
| **Clarifying questions** | When a request is ambiguous ("I was charged twice" with two qualifying merchants), the agent asks the customer once, through a second LangGraph interrupt, instead of guessing. The answer is masked like any message, and the operations agent runs again with it. |
| **Multi-agent** | Supervisor pattern: a policy agent (retrieval and grounded answers) and an operations agent (tools). Each worker returns to the supervisor. |
| **RAG** | **Chunking:** heading-aware, with stable section ids for citations. **Search:** BM25 (Arabic-normalised: diacritics, alef forms, taa marbuta, the definite article) plus dense vectors (Gemini embeddings, or an offline n-gram hasher), fused with reciprocal rank fusion. **Reranking:** by the model, falling back to the fused order. |
| **Output guard** | The final reply is scanned: any personal data (card, ID, IBAN, phone) or echo of the system prompt and the reply is replaced by the template. |
| **Hallucination control** | **Citations:** any section id that wasn't retrieved is dropped. **Numbers:** every number in the final reply must appear in the facts, otherwise a template is used. **Calculations:** done in code, never by the model (duplicates, the 60-day window, caps, refundable amounts). |
| **Tool safety** | **Customer:** fixed by the session; no tool takes a customer id. **Refunds:** the agent can only *propose* one. **Rules:** caps and windows are enforced in code. **Execution:** refunds carry an idempotency key, so retries and double clicks never pay twice. |
| **Concurrency** | Ten reviewers approving the same case at once pay once, and every one sees the final state (a per-case lock, with the ledger's idempotency key as the backstop across servers). Fifty customers filing about the same duplicate at once lead to exactly one refund. Both tested. |
| **Cost control** | At most 8 tool steps and a per-case token budget; past it, the loop stops and rules finish the case (tested with a model that never stops). 10 cases a minute per address. |
| **Prompt injection** | **Screen:** a pattern screen (English and Arabic) sends suspicious messages to a person. **Structure:** even a fully hijacked model cannot reach another customer or move money (a test plays exactly that). |
| **PII / PDPL** | Card numbers (Luhn-checked; the last four kept), Saudi ID and Iqama numbers, IBANs, phone numbers and emails are masked **before** any model or log sees them. Only the masked text is stored. |
| **Models** | One interface, with adapters for Gemini, Groq (OpenAI-compatible, so OpenAI too) and Claude, in plain HTTP. **Fallback:** rate limits and outages move to the next provider; a bad request does not. **Degradation:** if every provider is down, each node falls back to deterministic rules and cases still progress. |
| **Observability** | Every step records its node, latency, provider, model, tokens, tool calls, retrieved and cited ids, and fallbacks. The demo page shows the trace for each case. **OpenTelemetry:** with `OTEL_EXPORTER_OTLP_ENDPOINT` set, every case is exported as a trace (one span per step, with `gen_ai.*` attributes from the GenAI semantic conventions) to Langfuse, Grafana, Jaeger or Datadog, carrying ids and masked text only. |
| **Durable state** | **Checkpoints:** LangGraph checkpoints in SQLite, so a case awaiting approval survives a restart and a new process can resume it (tested). **Index:** a case index feeds the reviewer queue (`GET /api/cases?status=awaiting_approval`). |
| **Audit trail** | Every opening, proposal, decision and refund is written to an append-only, hash-chained log (database triggers block edits). `GET /api/audit/verify` recomputes the chain and names the first broken entry; a test edits one and catches it. |
| **Streaming** | `POST /api/cases/stream` sends each graph step as a server-sent event as it finishes, so the page shows the agent working live. |
| **Metrics** | `GET /metrics` serves Prometheus metrics: cases by status and intent, human decisions, model calls and tokens by provider, provider fallbacks, tool calls and errors, and time per node. |
| **MCP** | `python -m wakeel.mcp_server` serves the same four tools over stdio to any MCP client, with the same guarantees. |
| **Deploy** | **Docker:** non-root image. **Kubernetes:** probes, an HPA and Secrets. **Render:** a free blueprint. **CI:** tests, evaluations, a container smoke test and an image push. |

## Results

Offline, as run in CI (rules mode, no model). Run `python -m evals.run`.

**Retrieval** (30 questions, 20 English and 10 Arabic; gold = the policy section that answers it):

| | recall@1 | recall@3 | MRR |
|---|---|---|---|
| BM25 | 0.57 | 0.73 | 0.66 |
| Vector (offline n-gram hash) | 0.70 | 0.90 | 0.80 |
| Hybrid (RRF) | 0.60 | 0.90 | 0.73 |

Hybrid did **not** beat vectors alone at rank 1 with the offline embedder. I kept the measured numbers rather than tuning fusion weights to 30 questions. With `--model`, the same table is produced for Gemini embeddings and the model reranker.

**Agent** (16 end-to-end cases in English and Arabic, including a clarifying question, plus 12 prompt-injection attacks):

| Metric | Result |
|---|---|
| Intent accuracy | 16 / 16 |
| Correct decision (the right refund, correctly none, or the right question) | 16 / 16 |
| Wrong refund proposals | **0** (the build fails if not 0) |
| Attacks sent to a person by the screen | 8 / 12 |
| Unsafe outcomes from attacks (wrong refund, or money moved without approval) | **0** of 12 |

**What the tools caught while building it:**
- **The evaluation:** it found that the rules fallback proposed the Jarir duplicate whatever merchant the customer named (Starbucks, Nahdi, even a cancellation): 3 wrong proposals. Fixed by matching the merchant the customer named (English or Arabic); a regression test keeps it fixed.
- **The integration test against Daftar:** it hit Daftar's rate limit, because the ledger adapter made one API call per transaction (N+1). Fixed by caching captured transfers (they never change) and honouring `Retry-After`.

**Choosing a model, measured:** `python -m evals.compare` runs the same cases and attacks on each configured provider on its own, and reports intent and decision accuracy, wrong refunds, unsafe outcomes, p50 and p95 latency, tokens per case, and how often each fell back to rules. Model choice is a measured trade-off, made per task.

## Run it

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn wakeel.api:app --port 8000          # http://localhost:8000
.venv/bin/python -m pytest -q
.venv/bin/python -m evals.run                          # add --model with keys set
```

**Models are optional.** Without keys it runs in rules mode. With a free key in `GEMINI_API_KEY` and/or `GROQ_API_KEY` (see `.env.example`), the model path is used, Gemini first and Groq on failure.

**MCP** (for example, in Claude Desktop's config):
```json
{ "mcpServers": { "wakeel": { "command": "/path/to/.venv/bin/python", "args": ["-m", "wakeel.mcp_server"], "env": { "WAKEEL_CUSTOMER": "sara" } } } }
```

**With Daftar as the ledger:** run [Daftar](https://github.com/Zuhdiemara/daftar), then:
```bash
python scripts/seed_daftar.py http://localhost:8080   # prints DAFTAR_URL and DAFTAR_KEY
DAFTAR_TEST_URL=http://localhost:8080 python -m pytest tests/test_daftar.py
```

## Documents

- [Security review against the OWASP Top 10 for LLM applications](docs/security.md)
- [Architecture decisions](docs/decisions.md)
- [Engagement plan, from workshop to production](docs/engagement-plan.md)

## From demo to production (what I'd do for a client)

1. **State:** swap `InMemorySaver` for `langgraph-checkpoint-postgres`, so cases awaiting approval survive restarts and any replica can resume them.
2. **Identity:**
   - customers come from the bank's identity provider (the session fixes the customer id);
   - reviewers sign in with SSO and roles;
   - approvals are logged with who, when and why.
3. **Data residency (PDPL):**
   - models served in-region, for example a Saudi cloud region or a self-hosted open model, for any data that must stay in the Kingdom;
   - the masking layer stays in front of every model either way.
4. **Evaluations as a gate:**
   - grow the golden set from real, anonymised cases;
   - add an LLM-as-judge for tone and Arabic quality, checked against human ratings;
   - block releases on safety metrics.
5. **Observability:** export the trace spans to OpenTelemetry (Langfuse, Grafana or Datadog), with dashboards for cost per case, latency, fallback rate and the human override rate.
6. **Rollout:**
   - shadow mode first: the agent proposes and staff decide without seeing the proposal, and the two are compared;
   - then assisted mode;
   - automate only the classes of cases with near-perfect agreement.
