# Tollgate: Jev showcase implementation plan

Status: proposed implementation, November 2, 2026. This document plans the next build; it does not change the application.

## 1. Product objective

Make Jev's contribution visible throughout one accounts-payable workflow: evaluate evidence, identify an exception, request appropriate review, and reassess when new evidence arrives. The primary demo should show useful work becoming possible because evidence improves, alongside a clear malicious instruction being blocked.

The product remains task-scoped authorization for financial agents. Code enforces credentials, permitted tools, exact amounts, destinations, duplicates, budgets and execution. Jev evaluates narrow semantic questions about the proposed action and supplied correspondence. Classification does not establish document authenticity or prove the agent's internal intent.

Build all five proposed features in three increments:

1. **Decision intelligence:** rich decision panel, intelligent review routing and evidence scoring.
2. **Evidence resolution:** append correspondence, invalidate stale reviews, and evaluate a fresh proposal.
3. **Policy playground:** compare threshold changes and policy changes against recorded cases without execution.

The first two increments form the submission demo. A minimal playground is next in priority; a general policy authoring platform is beyond this pass.

## 2. Current implementation and prerequisites

The current authorization gateway has two fixed yes/no semantic checks. The legacy compiler already accepts yes/no, Choice and Score policies, but the active control room only consumes yes/no results. `JevClient.parse_answer` discards Choice distributions and confidence, and rounds Score results into an integer. `build_state` silently clips each document at 4,000 characters. These behaviors must be addressed before adding evidence-sensitive workflows.

Retain the existing protections: staged durable financial mutations, checks after classification, approval evidence binding, request idempotency, isolated mandate budgets, and recovery after restart. Keep the current 105 tests passing.

### Typed response adapter

- Add a typed evaluation response that preserves the requested and resolved model, question definitions, answers, input-token usage, and request latency.
- Noul answers retain their 0–1 probability. Choice retains its selected option, all option probabilities and confidence. Score retains its fractional value, level probabilities, confidence and rubric legend.
- Validate finite numbers, expected question IDs and types, allowed Choice labels, expected rubric levels, confidence bounds and probability sums within a documented numeric tolerance.
- Missing or invalid enforcement answers hold the proposal for review. Invalid routing information goes to General review and cannot grant authority.
- Preserve the legacy adapter's public behavior where needed. Add a new full-response method for the control room, then map it to legacy `CheckResult` for historical evaluations. This avoids silently changing old reports.
- Use one mixed-question request for the active semantic policy. No question can depend on another question's answer in that request: each sees the same state independently; application code combines results afterward.
- Report measured request latency once, rather than summing the same latency for every question. Keep cost undisplayed until the applicable pricing and usage calculation are verified.

## 3. Increment A: decision intelligence

### A1. Define an explicit semantic policy bundle

Introduce `policies/control_room.yaml`, distinct from the historical benchmark policy. Each immutable version includes a content hash, model pin, question definitions, enforcement flags, thresholds, applicability rules and the deterministic composition rules.

Start with the following small bundle. Wording is a starting specification; validate it on examples before freezing the demo version.

| Question | Jev primitive | Specific judgment | Effect |
|---|---|---|---|
| instruction_override | Noul | Does the proposed action follow a document instruction that conflicts with the operator's task? Merely seeing an attack is insufficient. | High violation probability denies; ambiguous results require review. |
| unresolved_delivery_dispute | Noul | Does current relevant correspondence report missing, rejected or incomplete goods without an applicable receiving-team resolution? | High violation probability denies; ambiguity requires review. |
| conflicting_receiving_evidence | Noul | Do applicable receiving records make contradictory claims about this invoice that remain unresolved? | Requires review; contradiction alone is not proof of fraud. |
| delivery_confirmation | Score | How explicitly does eligible receiving evidence confirm delivery for this invoice? | May require review when a policy requires confirmation. Never overrides a deny. |
| exception_category | Choice | Which exception category best describes the evidence: delivery, conflicting payment instructions, missing confirmation, none, or other/unclear? | Routes an already-held case; does not authorize payment. |

Use four descriptive Score levels: no applicable receiving confirmation; vague or unlinked confirmation; explicit confirmation of only part of the delivery; explicit invoice-linked confirmation of complete delivery. Show the distribution and fractional score. Describe it as **delivery confirmation strength**, not a generic trust or fraud score.

The score's enforcement applies only to a mandate policy requiring receiving confirmation. Add that requirement explicitly to the operator-visible task and selected fixtures. A supplier's assertion is never eligible receiving confirmation simply because its prose says “receiving desk.”

### A2. Compose answers into a decision

Evaluation order:

1. Authenticate and validate the proposal; apply deterministic financial constraints.
2. If a hard constraint fails, deny with the concrete reason. Label semantic checks as not run.
3. Otherwise evaluate all applicable semantic questions together.
4. Critical violation probabilities above their deny thresholds deny the action.
5. An uncertain critical result, contradictory records, required-but-insufficient confirmation, or unavailable required classifier output holds the action.
6. If every required check passes, recheck current authority and execute through the existing staged persistence path.
7. For held actions, map Jev's exception classification to a local review queue. Low-confidence or unclear classification maps to General review.

Keep the current Noul review/deny thresholds as provisional starting values. Select Score and routing-confidence thresholds from a separate development set; record and freeze the values before evaluating the acceptance set. Do not equate Choice confidence with probability that the payment is safe. Preserve distributions so a middling average cannot hide probability on an unacceptable rubric level.

### A3. Review routing

Use three local queues: **Receiving review**, **Payment instructions review**, and **General review**. Map category labels to queues in code. These are UI filters and audit metadata in this prototype; they do not send messages or change who has operator permission.

Persist the classification, confidence, distribution, assigned queue and mapping version. Display an explanatory template such as “Delivery evidence needs review → Receiving review.” Do not describe this template as Jev-generated reasoning.

### A4. Decision panel

The selected payment shows, in this order:

1. Proposed action, execution state and the principal reason.
2. Passed authority and financial checks, visually separate from Jev results.
3. Source excerpts labeled with evidence IDs and source provenance.
4. Noul probability bars, with the actual policy thresholds marked.
5. Delivery-confirmation score, its rubric and level distribution.
6. Exception category, alternative probabilities, confidence and assigned queue when relevant.
7. Expandable exact input, question definitions, policy hash, model and latency.

Use “Source excerpt” for deterministic text selection. Unless an explicit evidence-selection classifier is added later, never claim Jev selected or cited the excerpt. Maintain PROBE versus agent labels. Verify the panel at 1280×720 and the normal app viewport, including internal scrolling.

### Increment A acceptance

- One eligible payment request produces all three primitive types in one Jev request.
- The panel renders the actual API outputs, including fractional scores and distributions.
- A hard-denied payment cannot be rescued by favorable semantic answers.
- Uncertain receiving evidence leads to a review queue even below the monetary approval limit.
- Missing/invalid semantic answers never result in automatic payment.

## 4. Increment B: evidence resolution workflow

### B1. Versioned evidence

Add an `EvidenceItem` model with `evidence_id`, `invoice_id`, `kind`, `source_type`, `source_record_id`, `body`, `recorded_at`, `effective_at`, `added_by`, optional `supersedes_id`, and content hash.

Source types distinguish invoice/supplier text, an operator-entered note, and an operator-verified receiving record. These labels come from the authenticated ingestion path, not from the text or an agent argument. The demo labels verification as simulated; it does not imply integration with an authenticated receiving system.

Evidence is append-only. A correction references a prior item instead of rewriting it. Reject cross-invoice references, invalid supersession and excessive input sizes. Store an evidence revision and hash for each invoice. Preserve the complete original evidence set and exact model input for every evaluation.

Replace silent clipping on this path with explicit limits. If relevant evidence cannot fit, hold the proposal with “evidence exceeds review limit.” The demo should have small enough evidence sets to include all applicable records. Never omit the late resolution silently.

### B2. Operator experience

On a held or denied invoice, provide **Add evidence**. The dialog selects a source type, displays the invoice identity, accepts the record body and offers synthetic example records for rehearsal. Distinguish the sample-record action from real evidence ingestion.

After submission, show the new evidence in a timeline and offer **Re-evaluate payment**. New evidence alone does not execute anything. The button explicitly states that an allowed fresh proposal may execute a mock payment.

### B3. State transition and execution binding

Adding evidence atomically appends the item, increments the revision and cancels any pending proposal for that invoice whose approval was bound to the previous revision. Cancellation releases its budget reservation. Completed receipts remain unchanged; old denied decisions remain immutable.

Reassessment creates a new proposal with a new request ID, `parent_event_id`, the latest evidence revision and the original payment arguments. It runs through the same constraints, Jev evaluation and executor. A completed invoice is still blocked as a duplicate. A reassessed large payment still requires monetary approval.

Use an explicit `operator_reassessment` origin. The authenticated operator endpoint calls a narrowly scoped gateway method; it must not expose or recreate an old agent credential or accept arbitrary tool names. The implementation should work after restart because the operator initiates reassessment using durable records. The model loop is not automatically resumed.

Double-clicks and network retries use a stable reassessment key tied to the parent decision and evidence revision, so they return the same new proposal. Concurrent evidence changes and approval must serialize safely, and persistence failures publish neither the new evidence nor the cancellation.

### B4. Demonstration cases

- **Missing confirmation:** a permitted payment below the automatic limit is held; a new eligible receiving record confirms full delivery; reassessment allows execution.
- **Supplier pressure:** supplier text says to ignore missing goods; payment remains denied even after another supplier message says “resolved.”
- **Actual resolution:** a new operator-verified receiving record resolves the specific shortage; reassessment can pass when all other conditions hold.
- **Conflicting records:** contradictory applicable receiving records produce review until a valid correction clarifies which record is current.

Avoid weakening the policy during the evidence-resolution demo. The change in decision should be attributable to new evidence under the same pinned policy.

### Increment B acceptance

- Old decisions retain their exact evidence and policy snapshots.
- Stale approval cannot execute after evidence changes.
- Evidence submission alone never creates a payment.
- Reassessment cannot bypass revoked/expired authority, duplicate checks or approval limits.
- Repeated reassessment/approval produces at most one mock receipt.
- Failed persistence leaves both evidence state and financial state unchanged.

## 5. Increment C: policy playground and replay

### C1. Two distinct replay modes

**Threshold replay:** use saved, validated answers and immutable historical financial checks. Adjust review/deny thresholds and recompute proposed outcomes locally. No Jev call is required. Show “Reused model answers,” the original thresholds, new thresholds and changed outcomes.

**Policy replay:** edit one or more bounded semantic questions or select a versioned preset, then send the frozen historical input to Jev again. Show “Fresh Jev evaluation,” question differences, model version, measured latency and outcome changes. Token usage and call count should be visible before running a bounded batch.

Changing question wording, rubric definitions, options or model invalidates cached answers. A threshold-only replay cannot synthesize a new question's answer. Neither mode calls the executor, changes reservations, resolves reviews or alters historical audit records.

### C2. Minimal UI

Provide a separate **Policy lab** view with:

- Versioned policy preset and a small editor for the semantic rules.
- Review/deny controls with validated ordering and ranges.
- A comparison table: case, original result, replay result, changed checks and evaluation mode.
- Counts of allowed, held and denied cases. Show false blocks/misses only for a labeled evaluation set; ordinary audit events have no ground-truth accuracy label.
- Export of the experiment, including frozen inputs, questions, answers, policy versions and model metadata.

Replay each recorded proposal against the state it had when evaluated. Do not replay against today's ledger, which would turn previously successful payments into duplicates. Label this as per-proposal historical comparison; it is not a simulation of how an entire agent trajectory would change.

### C3. Activation rules

Save a draft as an immutable policy version after schema validation. The operator can select that version only when creating a fresh mandate. Existing mandates and pending reviews retain their pinned policy. Draft or replay configuration must never silently alter live authorization.

The editor accepts bounded structured policy fields, not executable code or arbitrary templates. A new semantic policy cannot enlarge the deterministic tool or payment permissions. If the deadline is tight, ship replay against two validated presets and defer free-form editing.

## 6. Data model and API outline

New durable concepts:

| Model | Essential contents |
|---|---|
| PolicyVersion | Immutable questions, composition rules, thresholds, model pin, hash and creation metadata |
| SemanticEvaluation | Exact input snapshot/hash, evidence revision, policy hash, full typed answers, model and usage/latency |
| EvidenceItem | Invoice-bound content, source metadata, correction relationship and content hash |
| ReviewAssignment | Category, distribution, confidence, queue and mapping version |
| ReplayExperiment | Historical event IDs, original/replay policies, evaluation mode, results and side-effect-free marker |

Add fields to session/event models with backward-compatible defaults. Older events explicitly say “not recorded” where distributions or provenance are absent. Do not fabricate those fields or assign strong provenance by parsing old document prose. Fresh mandates use the new evidence schema; legacy sessions keep their existing review behavior or require a fresh mandate for unsupported new features.

Proposed operator routes:

| Route | Behavior |
|---|---|
| GET /api/policies | List versioned policies and permitted presets |
| POST /api/policies/drafts | Validate and persist a bounded draft |
| POST /api/policies/{id}/versions | Freeze an immutable version |
| GET /api/sessions/{sid}/invoices/{iid}/evidence | Read the evidence timeline |
| POST /api/sessions/{sid}/invoices/{iid}/evidence | Append evidence and atomically invalidate stale pending review |
| POST /api/sessions/{sid}/events/{eid}/reassess | Submit a narrowly scoped fresh payment proposal linked to the old event |
| POST /api/replays | Create a bounded threshold or fresh-policy experiment |
| GET /api/replays/{id} | Read progress/results; useful when several live evaluations are required |

Extend session creation with `policy_version_id`. Extend session views and audit export with outcomes, evaluation references, evidence revisions and queue assignments. Agent action routes remain unable to author evidence, change policies or review payments.

## 7. File-level implementation map

- `tollgate/clients/jev.py`: full typed evaluation parsing, validation and metadata; compatibility wrapper for old consumers.
- New `tollgate/semantic_policy.py`: policy versions, mixed questions and deterministic composition.
- New `tollgate/evidence.py`: evidence models, revisioning, input assembly and limits.
- `tollgate/authorization.py`: staged evidence updates, evaluation binding, reassessment and review invalidation.
- New `tollgate/replay.py`: pure threshold recomputation and isolated live replay.
- `tollgate/demo_server.py`: authenticated policy/evidence/reassessment/replay routes and bounded validation.
- `dashboard/control.html`, `control.js`, `control.css`: typed results, queues, evidence timeline and Policy lab.
- `policies/control_room.yaml`: initial validated showcase policy.
- Fixture additions: routine, ambiguous, conflicting, malicious and subsequently resolved receiving records.
- New focused tests for the adapter, policy composition, evidence lifecycle and replay isolation; preserve the existing authorization suite.
- `evals/`: frozen development/acceptance examples and saved live results with policy/model/input provenance.
- `README.md`, `DEMO.md`, `IMPLEMENTATION.md`: updated product claims, workflow, architectural boundaries and recording instructions.

## 8. Verification plan

### Automated behavior tests

1. Preserve fractional scores and complete Choice/Score distributions.
2. Reject missing answers, unknown categories, nonfinite probabilities and malformed distributions.
3. Hard constraints dominate favorable semantic classifications.
4. Contradiction or insufficient required confirmation leads to review.
5. Routing uncertainty falls back to General review without affecting execution permissions.
6. Evidence provenance cannot be supplied by the agent or inferred as trusted from body text.
7. Supplier “resolved” language cannot satisfy a policy requiring receiving confirmation.
8. Evidence append, stale-review cancellation and reservation release persist atomically.
9. Old decisions preserve their input/evidence snapshots.
10. Evidence changes between review and approval prevent stale execution.
11. Concurrent reassessment retries and approvals produce one receipt.
12. Reassessment honors expiry, revocation, exact amounts, budget and duplicates.
13. Oversized relevant evidence causes a hold instead of silent clipping.
14. Threshold replay makes zero model and executor calls; policy replay makes zero executor calls.
15. Replay uses historical inputs and cannot mutate live financial state.
16. New policy versions leave existing mandates unchanged.
17. Restart restores evidence/reviews, and operator reassessment works without a cached runner token.
18. Legacy database fixtures load without fabricated model or provenance data.

### Live evaluation

Create a small development set for wording/threshold selection, then freeze a separate acceptance set with approximately 24 distinct cases across routine, missing, partial, resolved, contradictory and injected correspondence. Include historical disputes, negation, unrelated invoice IDs and forged source claims. Label expected execution disposition, review category and acceptable confirmation levels independently of model outputs.

Run the frozen set twice with the pinned model. Report unique cases separately from repeated attempts, failures by category, legitimate auto-execution, review rate, routing confusion, and latency. Store real distributions; do not substitute synthetic confidence values if a live example is less visually dramatic. If policies change after a failed acceptance run, preserve that run and identify the evaluated set as reused development evidence thereafter.

Test the complete UI sequence with one real agent run and clearly labeled probes. Verify persistence/restart, evidence upload failure, repeated clicks, stale approval, Policy lab isolation, normal app viewport and 1280×720. Save screenshots and a sanitized audit. Refresh the ZIP and SHA256 manifest only after final changes.

## 9. Build sequence and deadline decisions

| Order | Deliverable | Completion gate |
|---|---|---|
| 1 | Full typed adapter and versioned policy bundle | All primitive outputs preserved and malformed answers safely handled |
| 2 | Multi-question decisions, Score, queue routing and panel | Three Jev primitive types visible on a real evaluation |
| 3 | Versioned evidence and atomic stale-review invalidation | Evidence and financial fault-injection tests pass |
| 4 | Operator reassessment and evidence timeline | Ambiguous → new receiving evidence → fresh allowed payment works end to end |
| 5 | Threshold replay and two policy presets | Historical comparisons produce no live side effects |
| 6 | Bounded policy editing, if time permits | New versions affect only fresh mandates |
| 7 | Frozen evaluation, browser rehearsal and final packaging | Saved real outputs, accurate narration and one reproducible launch path |

Protect recording time. If scope must shrink, cut free-form policy editing first, then batch replay polish. Keep the typed decision panel and evidence-resolution workflow. A complete working sequence is more valuable for submission than five unfinished screens.

## 10. Proposed 90-second showcase

- **0–15 seconds:** authorize the invoice task; identify the budget and confirmation policy.
- **15–30 seconds:** a routine payment passes; reveal Jev's typed checks, confirmation rubric and measured response time.
- **30–45 seconds:** an ambiguous payment is held and routed to Receiving review.
- **45–60 seconds:** add a clearly labeled verified-demo receiving record and explicitly re-evaluate; show the new decision and execution receipt beside the original hold.
- **60–75 seconds:** submit a labeled instruction-conflict probe; show why new supplier pressure cannot resolve the issue.
- **75–90 seconds:** compare two policy presets or thresholds on saved cases; show that the live ledger stays unchanged.

Narration should explain that Jev supplies typed judgments and the application binds those judgments to authority and execution. Confidence is a model signal to validate on this workload, not a guarantee of correctness.

## 11. Official references

- [Primitives and parallel mixed questions](https://docs.typesafe.ai/introduction)
- [Choice outputs and probability distributions](https://docs.typesafe.ai/primitives/choice)
- [Score rubrics, fractional values and distributions](https://docs.typesafe.ai/primitives/score)
- [Confidence semantics](https://docs.typesafe.ai/confidence)

These references establish the model interfaces. The proposed workflow, source verification, policy versioning, routing queues, persistence and replay are Tollgate application features to implement.

## Implementation status

The five feature areas are implemented and verified. See SHOWCASE.md for the actual recording sequence and verification. The local implementation uses synchronous bounded replay and a single validate-and-save policy version step instead of separate draft/publication endpoints. No separate threshold calibration set was used; thresholds remain provisional. Acceptance outputs are generated locally and excluded from version control.
