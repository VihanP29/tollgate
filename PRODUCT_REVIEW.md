# Tollgate product review — November 2, 2026

## Assessment

The prototype supports a credible residency application demo: a real tool-using agent, explicit delegated authority, deterministic financial controls, live Jev semantic checks, human approval, and execution receipts. The strongest demonstration is a disputed invoice whose amount, beneficiary and budget all pass, but whose correspondence makes payment inappropriate.

The product is a local synthetic invoice-payment sandbox. It does not establish that banks will adopt it, eliminate hallucinations, prove internal intent, or provide production financial safety. The startup hypothesis is an authorization gateway for teams deploying financial agents; its current concrete workflow is accounts payable.

## Verification performed

- All 99 existing automated tests passed again, without paid model calls.
- JavaScript syntax and the shell launcher syntax passed.
- The running server on port 8765 returned a healthy response with the Jev backend configured. This health endpoint does not independently test model availability.
- Reviewed authorization, execution, persistence, runner, API, dashboard, launcher, evaluation and recording documentation.
- Ran isolated fault injections for expiry during classification, failed persistence, restart recovery and an agent returning no actions. These did not modify the running demo or application source.
- Reviewed previously saved live-run evidence and the evaluation design. No new live-model evaluation was run in this audit. Fresh browser inspection was unavailable because the browser-control tool returned an environment error.

## Findings and proposed work

### 1. Recheck authorization immediately before execution — high priority

Constraints run before semantic classification. Advancing a controlled clock past mandate expiry during classification still produces an executed payment. Recheck active authority and relevant constraints after classification and before mutation; record why any previously allowed proposal is stopped. Add a regression test with a delayed reviewer.

### 2. Make failed persistence leave no committed in-memory payment — high priority

The mock executor mutates the session before SQLite save. A simulated save failure leaves one payment in memory and zero in the durable snapshot. Retrying the same request returns the in-memory receipt without repairing persistence. Stage session changes, persist them, then publish the new state, or implement reliable rollback. Cover submissions and review resolutions with fault-injection tests. This addresses the mock executor only; external-bank atomicity remains future work.

### 3. Make restarts and launch recovery explicit — demo priority

Runner credentials are intentionally memory-only, while session state persists. After restarting, a completed session can still expose Challenge gate, but its probe returns HTTP 409 asking for a fresh mandate. Expose runner availability in session responses, disable unavailable actions, and provide clear fresh-mandate recovery. Consolidate launch guidance around the actual demo directory and printed URL; the current preview uses 8765 while the launcher defaults to 8000.

### 4. Separate agent termination from task completion — demo priority

A controlled planner returning no calls and “Done” produces status completed with zero events and zero payments. Track invoice outcomes independently of the model's stop reason. Display paid, awaiting review, blocked and unprocessed counts, and describe the agent as finished when it has merely stopped proposing actions. Approval executes the held payment; it does not resume the agent loop.

### 5. Make the AI contribution and startup use case immediately visible — polish

Present the receiving-team evidence beside the proposed payment and semantic verdict, with the passed financial checks visible. Use a short explanation of the target buyer and workflow. Distinguish the real agent, scripted rehearsal and explicit probes throughout the recording. Do not credit Tollgate for an attack the live agent declined to attempt.

### 6. Keep evidence claims narrow and package one final version — submission priority

The semantic acceptance set has 16 manually authored cases repeated twice, all focused on delivery correspondence. It demonstrates the intended behavior, not general security accuracy. Keep that qualification beside results. Duplicate prevention and budgets are scoped to each isolated demo session. Freeze one reviewed source version and refresh the ZIP after approved changes; currently the copied source is uncommitted. Keep a complete successful recording before another take.

## Proposed execution order, pending approval

1. Fix findings 1–4 with focused regression tests.
2. Apply the presentation changes in finding 5 and clarify session-scoped behavior.
3. Run the suite, verify restart and error recovery, rehearse the real-agent → labeled semantic probe → human approval → receipt sequence, and refresh the source package and recording guide.

Defer bank connectivity, multi-tenant authentication, broad benchmarks and a general-purpose policy editor until after submission. They are substantial follow-on projects and are not needed to demonstrate this narrower product convincingly.

No product implementation changes were made during this review. The local server was left running.

## Approved changes completed

The approved pass was implemented on November 2, 2026. Execution now rechecks authority after classification. Financial mutations and revocations are staged and published after persistence succeeds. Session responses expose runner availability and restart recovery. Invoice outcomes are derived from receipts and recorded proposals, while the runner reports finished rather than task completion. The dashboard shows a source excerpt beside semantic decisions, and the default server port and launch guide now use 8765.

Verification: all 105 tests passed, including six added regression cases; JavaScript and launcher syntax passed. Browser verification covered restart recovery, creating a fresh mandate, a real Claude/Jev run, a labeled dispute probe and human approval. No browser warnings or errors were captured. The fresh run paid three invoices automatically, held $38,000 for review, and skipped two invoices. The explicit dispute probe blocked one skipped invoice; approval then produced four total receipts worth $47,290, with one invoice still unprocessed. The exported audit matches those outcomes in the local live audit generated during review. This variability is reflected in the UI rather than represented as full task completion.

Screenshots: docs/polished-control-room.png and docs/polished-approval.png. The server remains running at http://127.0.0.1:8765/. SHA256SUMS records the packaged source version without including local credentials or runtime state.
