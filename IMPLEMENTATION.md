# The authorization product

Tollgate's current product hypothesis is a control layer for engineering teams deploying financial agents. An institution or platform would authorize a particular agent to perform a particular task, and the gateway would enforce that delegation at the point of execution.

This prototype demonstrates the boundary using synthetic invoice payments. It does not establish bank demand, institutional approval, regulatory compliance, or production safety.

## What the model is trusted to do

Jev answers two atomic semantic questions:

1. Does this proposed action follow a document instruction that conflicts with the operator's task?
2. Does supporting correspondence show an unresolved delivery dispute for the proposed payment?

Each returns a risk-aligned yes probability. Above 0.90, the action is denied; from 0.35 through 0.90 it requires review; below 0.35 it passes that check. These are prototype thresholds, not statistically calibrated guarantees. Missing answers, invalid probabilities and network/model errors require review, with no automatic execution.

The classifier does not grant permissions, verify identities, establish bank balances, calculate budget consumption, or prove an agent's internal intent. It evaluates supplied evidence and can be wrong.

## Authority and evidence

The operator creates a mandate through a separately authenticated API. Approved invoice amounts and bank destinations come from server-side fixture records. They are copied into the mandate when it is created. The task has a cumulative budget, an automatic-payment limit and an expiration time.

The agent receives a random session-specific credential. Only its hash is persisted. The action API accepts `request_id`, `tool` and `args`; extra top-level fields such as an agent-supplied `operator_request` are rejected. The server builds the context used for classification, including the trusted task, relevant invoice and recent recorded outcomes. Invoice text is labeled untrusted.

In the demo runner, the model sees tool schemas and tool results. It does not receive the operator credential or a tool that bypasses the gateway. This is an application/tool boundary, not operating-system sandboxing of arbitrary hostile code.

## Execution and review

The gateway serializes state changes with a lock. It checks permission and exact argument shape, then invoice identity, amount, beneficiary, pinned bank details, date, duplicates and budget. Money is compared and accumulated as integer cents. Pending payments reserve budget and prevent another pending payment for the same invoice.

For valid payment proposals, Jev evaluates semantic checks. Hard constraints are checked again after classification so expiry during the network call cannot authorize execution. An allowed action executes through the internal mock executor. A denied or escalated action cannot call it. Only read operations and invoice payments are implemented by that executor.

An identical request ID and identical call return the original decision and receipt. Reusing the ID for different arguments is rejected. Using a new ID for an already completed or pending invoice is denied.

Review holds the exact proposal and a hash of the mandate, beneficiary state and supporting document. Approval rechecks active authority, financial constraints and the evidence hash. If they changed, the action is cancelled. Otherwise the mock payment executes once. Repeated or concurrent approval requests return the same resolved result.

Human approval is an explicit exception to the original review requirement. It does not override a failed hard constraint, stale evidence, expiry or revocation. Original decision and execution status remain separate in the audit.

## Persistence and authentication

SQLite stores a session snapshot and an application-level append-only history within the same database transaction. The synthetic ledger is part of that snapshot. Payments, review resolutions and revocations are staged in a private session copy, persisted, and then published to the in-memory session. A failed save leaves the previously published financial state unchanged. Pending reviews and completed receipts survive restarts; interrupted agent runs are marked interrupted and are not automatically restarted.

The local operator uses a separate bearer token, supplied in a URL fragment and moved to session storage by the UI. The fragment is removed from the visible URL and is not sent as an HTTP query. Protected API calls use the Authorization header. Agent credentials cannot read operator session data, approve reviews, revoke mandates, run probes or export audits. Exports exclude credential hashes and credentials.

The server binds to loopback and uses no permissive CORS middleware. This is a single-user development authentication scheme. A real deployment needs institution-controlled identity, delegated authorization, appropriate secret management and isolation. Do not expose this local server publicly.

## Real agent versus attack probes

The real Claude agent can independently resist malicious invoices. Its generated summary can also disagree with its actual receipts; the ledger is authoritative.

To test the gateway independently, an operator can submit four fixed adversarial proposals after a run. Those probes use the same submission method and executor as agent calls. They are labeled `adversarial_probe` in persisted events and **PROBE** in the UI. The deterministic rehearsal is labeled `scripted`; real model proposals are labeled `live_agent`.

## Evaluation scope

`evals/control_eval.py` holds sixteen fixed, manually authored semantic acceptance cases. The eight risky and eight benign cases all have a valid amount, payee, destination and budget. Paired examples include incomplete versus completed deliveries, open versus resolved disputes, negated disputes, historical disputes and ordinary urgency.

The hard-controls-only baseline uses the identical gateway and executor while bypassing semantic review. This isolates the contribution of the semantic check. Two runs do not make the sixteen cases independent, and the set is not a held-out bank dataset. Treat the results as reproducible acceptance evidence, not general security accuracy.

The original `evals/data` corpus and saved legacy results are retained for historical provenance. Its template overlap and false-block limitations remain; the new gateway is not evaluated by the old report.

## Before any real financial deployment

The mock executor's atomic local snapshots do not solve atomicity with an external bank. A real adapter would need its own transaction binding, provider-supported idempotency, reconciliation of uncertain outcomes, and carefully defined authorization checks at execution time.

Further work includes independent evaluation data, adversarial tests of the reviewer, trusted data provenance, stronger institutional authentication, per-tenant isolation, production concurrency and operational monitoring. Classification is one control in that system. It is not a replacement for the other controls.

## Jev showcase implementation

The mixed-question path, versioned evidence, operator reassessment, policy editor and historical replay are implemented. See SHOWCASE.md for exact behavior and tested limitations. Legacy sessions and benchmark interfaces retain their earlier behavior. New API mandates default to the confirmation-required semantic policy.
