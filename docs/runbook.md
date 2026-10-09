# Runbook

What each alert in `ops/alerts.yml` means, and what to do first. Dashboard: `ops/grafana-dashboard.json`.

## Wakeel down
**Meaning:** Prometheus can't scrape `/metrics`.

**First steps:**
1. Run `kubectl get pods -l app=wakeel` and `kubectl logs deploy/wakeel --tail=100`.
2. A pod that refuses to start in production usually printed `Refusing to start in production` with the missing setting (see `wakeel/config.py`).
3. Check that Postgres is reachable.

## Slow
**Meaning:** the p95 of one graph step is above 20 s.

**First steps:**
- **Model steps** (`supervisor`, `policy_agent`, `ops_agent`, `respond`): check the provider's status and the *Tokens per hour* panel. A slow provider is skipped automatically only when it errors, so consider moving it down `WAKEEL_MODELS`.
- **`execute`:** check the ledger (Daftar).

## Backlog
**Meaning:** more than 50 cases are waiting for a worker.

**First steps:**
1. Scale workers: `kubectl scale deploy/wakeel-worker --replicas=N`, or raise `WAKEEL_WORKER_THREADS`.
2. Check *Model errors*: if every model is rate-limited, more workers make it worse.

## Dead jobs
**Meaning:** a case failed 3 times. It is marked `failed` and audited (`case_failed`).

**First steps:**
1. Read the error with `select * from jobs where state = 'dead'`.
2. A person handles the customer's case directly.
3. After a fix, requeue it with `update jobs set state = 'queued', attempts = 0, run_at = 0 where id = …`.

## Models
**Meaning:** model calls are failing, or the agent is running on rules.

**What the agent does:** every case still progresses. Rules finish the step, and safety checks don't depend on the model.

**First steps:**
1. Check quotas and provider status.
2. The trace and `llm_errors` on each step name the error, for example `HTTP 429` or `model_not_found` after a model is retired.
3. Update `WAKEEL_MODELS`.

## Cost
**Meaning:** more than 2M tokens in an hour.

**First steps:**
1. Look for one customer opening many cases. The per-customer limit is `WAKEEL_CASES_PER_MINUTE`.
2. Look for a tool loop, where `ops_agent` spans show `over_budget`.
3. Lower `token_budget` if needed.

## Injection
**Meaning:** many messages were flagged as prompt injection in an hour.

**What the agent does:** flagged messages go to a person, and the structural guards hold either way.

**First steps:**
1. Check whether one customer is responsible; if so, block them at the identity provider.
2. Add new attack phrasings to `evals/injection.jsonl` so the evaluation covers them.

## Drift
**Meaning:** either the rules keep raising refunds the model missed (cross-checks), or reviewers reject more than 20% of proposals.

**First steps:**
1. Sample the cases.
2. Run `python -m evals.compare` and `python -m evals.judge` against the current models.
3. A provider may have changed its model. Pin a version, or switch.
