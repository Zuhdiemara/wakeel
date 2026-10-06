# From workshop to production: delivering a disputes agent for a bank

How I would run this as a client engagement. The timings are typical, not promises.

## Weeks 0–1 · Discovery workshop
- **Process:** map the dispute process today: volumes per dispute type, handling time, backlog, error and complaint rates.
- **Scope:** pick one narrow, high-volume, low-risk intent (duplicate charges).
- **Exclusions:** fraud stays with people.
- **Success metrics, agreed with the business:**
  - share of eligible cases resolved without rework;
  - wrong-refund rate (target 0);
  - time to decision;
  - customer satisfaction.
- **Access:** policy documents, transaction read API, refund API (is it idempotent?), and a sample of past cases with outcomes.
- **Risk:**
  - PDPL data classification;
  - data residency (which models may process which data);
  - SAMA outsourcing and cloud expectations;
  - model risk management owners.

## Weeks 2–4 · Proof of concept
- **The full loop on anonymised data:** intake, retrieval, tools, the approval screen, a refund in a sandbox.
- **Model comparison:** two or three candidate models compared with `evals.compare`, including in-region options.
- **Golden set:** built from historical cases, in Arabic and English.

## Weeks 5–6 · Evaluation and security review
- **Report:** decision accuracy, wrong actions, injection results, Arabic quality, latency and cost per case.
- **Security:** an OWASP LLM Top 10 review (see `security.md`) and a red-team session in Gulf Arabic.
- **Go or no-go** with the business, risk and compliance.

## Weeks 7–10 · Pilot in shadow mode
- **Shadow mode:** the agent proposes, staff decide without seeing the proposal, and the two are compared.
- **Integration:** SSO and roles for reviewers, the Postgres checkpointer, audit export to the bank's systems, dashboards (cost, latency, fallback rate, agreement with staff).

## Weeks 11+ · Assisted mode, then selective automation
- **Assisted mode:** staff see the proposal and the evidence, and approve.
- **Selective automation:** automate only case classes with near-perfect agreement, keeping caps and sampling for review.
- **Handover:** runbooks, evaluation-in-CI as a release gate, and training for the bank's team.
