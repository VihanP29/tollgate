# Tollgate Policy Studio — implementation plan

Status: proposed; this document does not authorize or implement product changes.

## 1. Outcome

Build one Policy Studio where an operator can configure financial controls, edit the actual typed questions sent to Jev, compare a draft against the active policy, and activate a tested version for new mandates. An auditor can inspect the exact configuration, evidence, model output, and deterministic rule that produced a decision.

The demonstration should make the division of responsibility visible: Jev classifies evidence into typed answers; Tollgate applies explicit financial and semantic rules before permitting mock execution. Typed output constrains the answer format; it does not establish that the classification is correct or eliminate hallucination.

Keep this a focused extension of the existing local sandbox. Retain the five current question roles and the current financial workflow. Do not build a general workflow designer, arbitrary code editor, or production banking integration.

## 2. What exists and what changes

| Area | Existing behavior | Planned behavior |
| --- | --- | --- |
| Policy interface | Policy Lab modal with JSON editing and replay | Dedicated Studio with forms, typed question editors, and a comparison workspace |
| Financial configuration | Budget and automatic approval limit entered when creating a mandate | Versioned defaults and limits configured alongside semantic rules; resolved values pinned at mandate creation |
| Jev questions | Five fixed contracts; instructions editable; criteria fixed | Instructions, Noul criteria, Choice descriptions and categories, and Score rubrics editable with validated mappings |
| Semantic rules | Several global thresholds and hard-coded composition rules | Explicit per-question interpretation, thresholds, and routing configuration |
| Versioning | Content-hashed saved policies | Mutable drafts, immutable saved versions, comparison records, activation history, and an active-version pointer |
| Comparisons | Reuse semantic answers or reevaluate saved state with Jev | Baseline versus draft on identical scenarios, exact request diff, rule trace, and eligibility checks for cached answers |
| Financial replay | Saved nonsemantic outcomes are reused | New events record the financial inputs needed for deterministic historical comparison |
| Provenance | Policy and Jev state included in evaluation records | Add compiler version, configuration hashes, comparison provenance, and activation attribution |

The current enforcement implementation lives in `authorization.py`, `semantic_policy.py`, and `clients/jev.py`; replay is in `replay.py`. Extend these paths instead of implementing a separate decision engine in the browser.

## 3. Screen and interaction design

Add a **Policy Studio** entry beside the Control Room. Keep the Control Room for operational execution and evidence review. The Studio has a version selector, draft status, and three areas:

1. **Configure:** tabs for Financial controls, Jev questions, and Decision rules.
2. **Test:** a scenario selector, synthetic evidence editor, and explicit Compare button.
3. **Inspect:** baseline/draft decisions, typed answers and distributions, rule explanations, and the exact compiled request.

The version header shows active version, draft parent, unsaved changes, validation status, and whether comparison results match the current draft. The actions are **Save draft**, **Compare**, **Save version**, and **Activate for new mandates**. Editing never changes an existing mandate.

### Financial controls

Expose mandate budget, automatic approval limit, and receiving-confirmation requirement with clear units and explanations. Use integer cents internally; require positive budget and an automatic limit between zero and the budget. Show the budget and automatic limit before mandate creation and pin their resolved values to the mandate.

For the first release, policy values supply the mandate defaults and maximum permitted limits. A mandate can choose stricter values. A looser limit requires a new policy version. Explicitly define zero automatic limit as requiring review for every positive payment.

Show approved invoice amount, approved destination, duplicate-payment protection, mandate scope, expiry/revocation checks, and budget reservation integrity as protected controls. They are inspectable but cannot be disabled through the editor. Prompt edits cannot override them.

### Jev question editor

Start with the five existing question roles: instruction override, unresolved delivery dispute, conflicting receiving evidence, delivery confirmation, and exception category. Keep their IDs, types, and tool scope fixed in the first release. Display the real API primitive name **Noul** alongside the existing internal `yes_no` representation.

| Editor | Editable fields | Required validation |
| --- | --- | --- |
| All questions | Instructions and examples | Nonempty, bounded text; exact compiled preview; no template code execution |
| Noul | True/false criterion descriptions and interpretation of true | Explicit polarity: true indicates a violation or satisfies a requirement; normalized adverse probability shown |
| Choice | Category keys, descriptions, and review-queue mappings | Unique stable keys, complete mapping, explicit fallback; removing a key invalidates affected comparisons |
| Score | Ordered level descriptions and decision thresholds | 2–10 levels, contiguous indices, explicit direction and acceptable levels; rubric edits require threshold reconfirmation |

Choice remains a routing decision in the first release. A category cannot grant authorization or override another failing check. Map uncertain or unmapped routing results to General review; reject malformed model answers rather than treating them as a valid fallback.

For Score, configure both the minimum score and the acceptable levels used for probability-mass checks. Remove the current implicit assumption that level 3 always means acceptable. Preserve current behavior when migrating the existing four-level rubric. A rubric change invalidates cached answers even when its number of levels stays the same.

For Noul, convert the raw probability into the configured adverse probability before applying thresholds. Display the conversion, for example `P(required condition absent) = 1 − P(true)`. Preserve the contradiction check's current review-only behavior in the initial release; it should not silently acquire denial semantics.

### Shared guidance and exact request preview

Provide a **Shared evaluation guidance** field compiled into each question's instructions. Label it as Tollgate configuration. Jev's documented System One request exposes `state`, `model`, and `questions`; it does not document a separate chat-style `system_prompt` field. Do not invent such a parameter or expose unsupported temperature controls.

Keep the model pinned to the currently integrated `jev-1.14.0` for this release. Show it read-only, together with the model identifier returned by the API.

Separate application-owned source/provenance rules from editable evaluation guidance. Trusted context construction and source eligibility remain enforced by Tollgate code. Instructions alone do not make supplier text trusted.

The preview shows the exact outgoing `state` and typed `questions`, including compiled guidance and criteria, plus the applied configuration hash. Include a JSON view and a read-only enforcement explanation. There is no generated code to run. Question answers remain independent within a mixed Jev request; compose their outcomes in Tollgate afterward.

### Context and scenario editor

Allow edits to synthetic invoice correspondence, receiving evidence, proposed action, and synthetic financial state. Clearly label these as test fixtures. Do not allow a scenario edit to append operational evidence or change an actual mandate.

Display historical captured context read-only. Cloning it creates a new synthetic scenario with its own identifier and hash. Source type, invoice linkage, and supersession belong to structured fields, not instructions inferred from document text.

## 4. Configuration and persistence

Introduce a versioned `PolicyBundleV2` model separate from the legacy `Policy` type. Use strict schemas, reject unknown fields, and provide adapters where the existing client expects legacy objects.

The bundle contains:

- `schema_version`, display name, and optional description.
- Financial defaults and permitted limits.
- Pinned model and shared evaluation guidance.
- Five typed question definitions, including Noul criteria and complete Choice/Score contracts.
- Per-question decision settings, receiving-confirmation requirement, and queue mappings.
- Compiler version and protected-control contract version.

Use distinct hashes for the immutable bundle, exact compiled Jev input, and deterministic rule configuration. Do not include mutable lifecycle metadata in the content hash. Bind comparisons to the exact bundle hash, scenario-set hash, compiler version, and returned model identifiers. Hashing establishes identity, not correctness.

Add persistent records for drafts, saved versions, comparisons, and activation events. A draft has an ID, parent version, revision counter, author, and timestamps. Saved versions are immutable. Activation updates an active-version pointer and appends an audit record in one database transaction. Use expected revision/active-version checks to reject stale concurrent writes.

Record actor identity from the authenticated server principal, not a browser-supplied name. In the current single-operator sandbox, identify it honestly as the local operator. A read-only auditor credential can inspect versions, comparisons, and receipts; enforce those permissions on API routes. Do not represent a UI role toggle as access control or imply enterprise identity verification.

### Migration and compatibility

Convert existing saved policies and presets through a deterministic adapter, preserving current instructions, criteria, thresholds, and composition behavior. Populate new financial defaults from the current demo defaults for new mandates only.

Existing mandates retain their original limits and semantic policy. Existing receipts remain readable, including legacy records without a policy bundle. Store both the resolved mandate limits and bundle reference on new mandates. Creating a mandate and resolving the active version must be atomic so an activation race cannot produce a mixed configuration.

Old saved versions remain available for inspection and comparison. Reverting means reactivating a previously tested immutable version for future mandates, with a new activation event. It does not rewrite history.

## 5. Comparison engine

Provide two clearly named comparison modes:

**Reapply rules:** reuse model answers only when the model request contract, compiled guidance, criteria, and scenario state are identical. Changes to financial limits, decision thresholds, or routing mappings can use this mode where all required historical inputs exist. Show zero new model calls.

**Evaluate with Jev:** make fresh calls for changed instructions, criteria, model configuration, or evidence. Both versions receive the same scenario input. A stored baseline answer may be reused only when its complete input identity matches; otherwise evaluate the baseline too. Mark reused versus fresh answers and expose usage and latency. Never send requests on each keystroke.

Each row shows original operational outcome if applicable, baseline comparison outcome, draft outcome, queue changes, typed outputs, and the deterministic checks responsible for the result. Explain that model variation can affect fresh comparisons; a changed answer is not proof that a prompt change caused an improvement.

### Financial state capture

Extract reusable financial-check evaluation from the gateway without moving execution into replay. New decision records capture the relevant pre-decision state: mandate limits and status, evaluation time, spent/reserved budget, duplicate-payment status, approved invoice amount, destination reference, and action scope. Capture this under the gateway's existing synchronization boundary.

Evaluate baseline and draft against the same captured state. Historical events without sufficient financial state support semantic-only comparison and must say so. Never substitute current ledger balances for missing historical balances.

Version one compares scenarios independently. It does not simulate a sequence in which one hypothetical payment changes the budget for later rows. Clearly state that limitation in batch results.

### Execution isolation and failures

Comparison must not call payment execution, append operational evidence, cancel reviews, or reserve budget. Persist only experiment records. Verify this with before/after database assertions.

Use an explicit bounded batch size and show estimated call count before starting. The initial demo can use synchronous, small batches with a configured timeout; do not promise background cancellation or resumability until implemented. Preserve successful rows and identify failed rows; incomplete comparisons cannot qualify a version for activation. Operational classifier failures retain the existing fail-closed review behavior.

## 6. Save and activation lifecycle

1. Clone a preset or saved version into a draft.
2. Validate financial fields, typed contracts, thresholds, and mappings on the server.
3. Compare the draft against its selected baseline on the required synthetic regression suite; add custom scenarios as desired.
4. Save an immutable version and attach comparison records that match its exact content hash.
5. Activate the version for new mandates. The server checks schema validity, complete mappings, current comparison provenance, and the required suite's expected outcomes.
6. Create a new mandate and show the pinned version. Existing mandates continue using their previous settings.

Drafts and versions may be saved before passing tests, but cannot be activated through this workflow. Any edit invalidates prior activation eligibility for that draft. This is a proposed product behavior, not a claim that the suite certifies financial safety. Keep activation as a deliberate button with a clear scope label; no extra approval dialog is needed for the local sandbox.

## 7. API and code changes

Use the existing operator authentication and application database. Proposed endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/policy-studio` | Active version, presets, versions, permissions, editor constraints |
| `POST /api/policy-drafts` | Clone a version into a draft |
| `PATCH /api/policy-drafts/{id}` | Save with expected revision |
| `POST /api/policy-drafts/{id}/validate` | Return field errors, compiled request preview, and hashes |
| `POST /api/policy-comparisons` | Compare exact draft/version revisions on scenarios |
| `GET /api/policy-comparisons/{id}` | Inspect/export a persisted experiment |
| `POST /api/policy-versions` | Save an immutable validated bundle |
| `POST /api/policy-activations` | Activate with expected active version and qualifying comparison |

Keep existing session, evidence, and replay routes compatible. Centralize activation rules so legacy save endpoints cannot bypass them or silently change the active version.

Suggested implementation ownership:

- New `policy_config.py`: V2 schemas, migration, canonical hashes, compilation and validation.
- New `policy_store.py`: drafts, immutable versions, comparisons and activation transactions.
- `semantic_policy.py`: composition driven by validated typed configuration.
- `clients/jev.py`: Noul criteria support and request/response validation against the selected contract.
- `authorization.py`: bundle pinning, financial snapshots, shared pure checks, unchanged execution boundary.
- `replay.py`: comparison modes and strict answer-reuse eligibility.
- `demo_server.py`: authenticated routes, permissions, conflict handling and active-version resolution.
- `dashboard/`: Studio interface integrated with the current visual style; keep editor state separate from Control Room state.

## 8. Implementation order and acceptance gates

### Phase 1 — configuration foundation

Implement schemas, compilation, migration, hashing, persistence, and mandate pinning. Preserve every current preset decision before adding editor features.

Acceptance: existing tests pass; invalid contracts are rejected; immutable versions survive restart; activation and mandate creation are race-safe; old sessions are unchanged.

### Phase 2 — complete Jev editor

Build the dedicated Studio, financial forms, five typed editors, shared guidance, inline validation, and exact request preview. Add plain-language interpretation beside thresholds and distributions.

Acceptance: every exposed field reaches the compiled request or deterministic rule configuration; there are no decorative controls. Noul polarity, Choice mappings, and Score rubric changes validate correctly. Unsaved edits are visible and protected from accidental navigation loss.

### Phase 3 — comparisons and explanations

Implement state capture, synthetic scenario editing, compatible answer reuse, fresh evaluation, and baseline/draft traces. Show the precise reason a decision changes.

Acceptance: no comparison mutates operational state; financial snapshots reproduce original checks; legacy limitations are explicit; changing any model-input field invalidates answer reuse; failures and partial results remain inspectable.

### Phase 4 — tested activation and audit

Wire regression eligibility, immutable save, activation, rollback, auditor read-only access, and the new-mandate version badge. Add activation and comparison export.

Acceptance: stale or incomplete comparisons cannot activate a draft; unauthorized writes fail server-side; concurrency conflicts preserve both users' work; active pointer and audit history survive restart.

### Phase 5 — demo verification and polish

Run focused integration tests and a small live Jev acceptance run, then verify the entire recording sequence in the browser. Check loading/error states, keyboard access, overflow, restart behavior, and exact request visibility. Update the recording guide and package only after acceptance passes.

For the submission deadline, prioritize one complete vertical path across all five phases. Defer a generalized question builder, configurable model catalog, arbitrary context templates, enterprise SSO, multi-person approval workflows, asynchronous large evaluations, and live bank integrations. Do not trade away accurate comparison provenance or mandate pinning for extra controls.

## 9. Verification matrix

| Test group | Essential cases |
| --- | --- |
| Configuration | Unknown fields; invalid cents; automatic limit above budget; NaN/infinity; reversed thresholds; duplicate Choice keys; missing queue mapping; invalid rubric size; polarity conversion |
| Compilation | Shared guidance included exactly once; Noul criteria transmitted; full Score/Choice contracts retained; no invented API fields; source provenance retained |
| Caching | Instruction, criterion, guidance, evidence, or model changes force fresh evaluation; compatible threshold-only edits reuse answers |
| Enforcement | Exact threshold boundaries; protected checks dominate semantic passes; lower automatic limit produces review; over-budget action denied; pending reservations counted |
| History | Existing mandates pinned; deterministic migration; legacy semantic-only replay; no use of current balance in historical comparison |
| Lifecycle | Stale draft revision; stale comparison; concurrent activation; failed transaction rollback; restart; reactivation of prior version |
| Isolation and access | Experiments create no payment/reservation/evidence mutation; auditor cannot edit or activate; agent cannot access operator mutation routes |
| Model behavior | Missing confirmation; supplier-only assertion; partial receiving; complete acceptance; unresolved dispute; active contradiction; superseded correction; instruction injection |

Use the current synthetic acceptance fixtures as a starting point, extended for editor-specific changes. Report fixture counts and observed results, not a generalized accuracy claim. A fractional Score or a confidence value is not a calibrated probability that the decision is correct.

The offline backend must not pretend to interpret arbitrary edited prompts. Either restrict it to explicitly supported fixtures/contracts or mark such experiments unavailable offline. Live Jev runs and fixture demonstrations must remain visibly distinguishable.

## 10. Demo story

1. Open the active policy and show its financial controls, five typed Jev questions, and exact compiled request.
2. Clone it. Enable the receiving-confirmation requirement and compare the missing-confirmation fixture: the draft holds the action for review while the baseline allows it, provided all other controls pass.
3. Open the Score editor and show how its rubric and minimum acceptable result connect to that hold. Make a meaningful criterion clarification, evaluate with Jev, and inspect the returned distribution. Show the actual result even if the final verdict remains unchanged.
4. Add complete eligible receiving evidence to the synthetic scenario and evaluate again. Inspect the evidence, typed answers, and rules together.
5. Save and activate the tested version. Create a new mandate showing its pinned version; an existing mandate still displays its original policy.
6. Return to the Control Room for operational evidence/reassessment if demonstrating execution. The Studio comparison itself never sends a payment.

This demonstrates an operator-controlled Jev integration: configurable typed classification, visible evidence, deterministic authorization, and an auditable path from experiment to activation.

## Jev references

- [System One API](https://docs.typesafe.ai/api/system-one): request fields and question contracts.
- [Introduction](https://docs.typesafe.ai/introduction): mixed independent typed questions against shared state.
- [Choice](https://docs.typesafe.ai/primitives/choice): category descriptions, probabilities and confidence.
- [Score](https://docs.typesafe.ai/primitives/score): ordered criteria, fractional outputs and distributions.
- [Confidence](https://docs.typesafe.ai/confidence): interpreting model confidence.

## Implementation record — November 2, 2026

Implemented the focused Studio workflow: strict V2 bundles and legacy adapters; financial limits; five typed question editors; shared guidance and Noul criteria; configurable Score acceptance and Choice routing; compiled request preview; drafts with revision conflicts; immutable versions; independent baseline/draft comparisons; historical financial snapshots; exact-input answer reuse; required-suite activation; persisted activation history; mandate pinning; saved-experiment browsing/export; and optional auditor read-only access.

Verification: 140 automated tests passed (126 existing plus 14 Studio cases), JavaScript syntax checks passed, and two live four-scenario comparisons completed with 16 Jev calls. All eight draft outcomes matched the required suite. Browser verification covered editing guidance and criteria, saving, comparing, activation and reopening results. Generated live traces are excluded from the source repository.

Scope limits: five fixed question roles; pinned model; small synchronous batches (1–8 scenarios); independent financial comparisons; no production identity provider. Historical events lacking snapshots retain explicitly labeled semantic-only comparisons. Arbitrary edited prompts require live Jev. Initial regression coverage uses synthetic fixtures and provisional thresholds.
