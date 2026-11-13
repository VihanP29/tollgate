# Tollgate

**Task scoped authorization for financial AI agents.**

Tollgate gives an agent a specific task, an approved invoice set, and a budget. It checks each proposed tool call against that authority, enforces deterministic financial controls, and uses Jev for narrow semantic checks before a mock executor can act.

![Tollgate control room](docs/jev-evidence-resolved.png)

This is a local prototype with synthetic data and a mock financial executor. It does not connect to a bank or move real money.

## What the demo includes

- A Control Room for creating mandates, running an agent, inspecting decisions, reviewing exceptions, and inspecting mock payment receipts.
- Jev classification using three Noul probabilities, a delivery confirmation Score, and an exception routing Choice in one request.
- Evidence timelines, review queues, and explicit reassessment after an operator adds receiving evidence.
- A Policy Studio for editing financial limits, Jev question instructions and criteria, decision rules, and Choice routing. It can compare a draft against a saved policy, inspect the compiled Jev request, save versions, and activate a tested version for new mandates.
- A deterministic scripted rehearsal and an optional live Claude agent. Adversarial challenge probes use the same authorization gateway and are labeled separately.

## Run locally

Requirements: Python 3.12 or newer. From the project root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m tollgate.demo_server
```

Or, after creating the environment, run `./Launch Tollgate.command` on macOS. Keep the terminal open and use the private operator URL printed when the server starts. Opening the HTML file directly does not start the server.

No API keys are needed for offline mode. Without `TYPESAFE_API_KEY`, the app uses explicitly labeled fixture checks. This is a rehearsal mode and does not interpret arbitrary edited prompts. To enable live Jev classification, set `TYPESAFE_API_KEY` in `.env`. To enable the live Claude agent, set `ANTHROPIC_API_KEY` too. Live calls may incur provider charges.

Choose the backend explicitly if needed:

```sh
.venv/bin/python -m tollgate.demo_server --backend offline
.venv/bin/python -m tollgate.demo_server --backend jev
```

The server listens only on `127.0.0.1:8765`. The operator link is a local access credential; keep it private. Runtime state is saved to `.runtime/control.sqlite` and excluded from Git. Use `--port 8766` if port 8765 is occupied. After restarting, saved receipts and pending reviews remain available. Create a new mandate to run an agent or challenge probes again.

## Demo walkthrough

1. Create a mandate with the $60,000 task budget, $10,000 automatic payment limit, and approved invoices. The operator pins the approved invoice amount and beneficiary bank details.
2. Run **Scripted rehearsal** for predictable behavior. With both API keys configured, select **Claude · live agent** to make real Anthropic and Jev calls; financial tools remain mocked.
3. Open **Policy Studio**, clone a policy, and inspect Financial, Jev questions, and Decision rules. Edit shared guidance or a typed question, then preview the compiled request.
4. Compare the draft against the **Dispute controls** baseline. The four synthetic cases cover complete receiving evidence, missing confirmation, supplier-only claims, and unresolved disputes. Inspect typed answers, financial checks, and the exact request.
5. Save the immutable version and activate it for new mandates. Existing mandates keep their pinned policy. Experiments do not execute payments or reserve budget.
6. Return to the Control Room to inspect evidence, submit a labeled adversarial probe, or review a pending decision. Approval rechecks current authority and evidence before it records a mock receipt.

The live agent may reject suspicious document instructions itself. Attribute to Tollgate only decisions its gateway records. Counts distinguish paid, awaiting review, blocked, and unprocessed invoices; an agent finishing means it stopped proposing actions, not that every invoice was paid.

## How authorization works

The agent cannot grant itself permissions or provide the trusted operator request. Its API accepts a request ID, tool, and arguments and requires a session specific credential. Operator routes require a separate local credential.

The gateway checks:

- Active, unrevoked, expiring authority and permitted tools.
- Exact match to the approved invoice amount and beneficiary.
- Pinned bank account and routing details.
- Cumulative task budget, including amounts reserved for pending reviews.
- Duplicate payments across distinct request IDs, with idempotent retries for the same request.
- Jev classifications for conflicting document instructions and delivery exceptions.
- Human approval bound to the reviewed action and evidence, followed by fresh constraint checks.
- Fail closed behavior when semantic review is unavailable or uncertain.

The mock executor implements reads and invoice payments. Vendor edits and exports have no execution path under this mandate. SQLite records mandates, proposals, decisions, review resolutions, and receipts. The agent's summary is not the execution record.

```mermaid
flowchart LR
  O[Operator mandate] --> G[Authorization gateway]
  A[Agent tool proposal] --> G
  G --> H[Identity and hard constraints]
  H --> J[Jev semantic checks]
  J --> V{Verdict}
  V -->|Allow| E[Mock executor]
  V -->|Review| R[Operator approval and recheck]
  R --> E
  V -->|Deny| B[Blocked]
  E --> L[Ledger receipt and audit]
```

## Evaluation and limitations

The project includes a synthetic semantic acceptance evaluator and sample datasets. A prior run used 16 manually authored cases twice: eight routine payments and eight payments conflicting with unresolved delivery evidence. The reported run stopped 16/16 risky attempts with Jev, compared with 0/16 for a permissions only baseline; both systems auto executed 16/16 legitimate attempts. These were repeated examples, not 32 unique cases, a held out bank benchmark, or a production accuracy claim. Thresholds are provisional, model probabilities are not guarantees, and pricing has not been verified. Generated run results are deliberately excluded from this repository; use the evaluator code to produce a new local run.

The system is a prototype, not production banking infrastructure. Identity uses local bearer tokens, the ledger is synthetic, and runtime state is stored in a local SQLite database. It has no enterprise identity provider or live bank integration. Policy comparisons use small independent scenarios; they are not a sequential simulation of how hypothetical payments change later budget balances. Historical events without captured financial snapshots support semantic only comparisons. Offline mode cannot interpret arbitrary edited prompts.

## Development and verification

Install dependencies as described above. Run tests and frontend syntax checks from the project root:

```sh
.venv/bin/python -m pytest
node --check dashboard/control.js
node --check dashboard/studio.js
```

The tests use mocked clients or explicitly labeled offline fixtures; they do not require paid API calls. The live evaluator does make provider calls and can incur charges:

```sh
.venv/bin/python -m evals.control_eval --live --env-file .env --runs 2
```

The evaluator writes generated output locally; those result files are ignored by Git.

## Project map

- `tollgate/authorization.py`: mandates, credentials, constraints, execution, reviews, and audit persistence.
- `tollgate/semantic_policy.py` and `tollgate/policy_config.py`: Jev questions, policy validation, and decision composition.
- `tollgate/policy_studio.py`: policy drafts, comparisons, versioning, activation, and auditor read access.
- `tollgate/clients/jev.py`: Jev API client and typed response validation.
- `tollgate/control_runner.py`: scripted rehearsal and Claude tool loop.
- `tollgate/demo_server.py`: local server and authenticated API routes.
- `dashboard/`: Control Room and Policy Studio interface.
- `policies/`: policy examples and readable policy snapshots.
- `tests/`, `agent/tests/`, and `evals/tests/`: automated checks.
- `evals/`: evaluator code and synthetic datasets. Generated reports are ignored.
- `examples/`: small agent integration example.

The legacy engine and benchmark interfaces remain for provenance and are separate from the current authorization gateway.

## Workspace platform: implemented offline path

The project now also has a relational workspace application at `/workspace`, with `/api/v1` contracts. This is an incremental implementation of the handoff, **not completion of Releases A and B**. The existing Control Room and Policy Studio remain available through the original launcher and retain their separate local prototype database. Their credentials and ledger are not automatically imported into a workspace.

The workspace path includes:

- Server-side membership checks, roles, isolated synthetic companies, and hashed, expiring, revocable mandate-scoped agent credentials. Hosted demo identities have bounded demo permissions and cannot administer memberships.
- Integer USD minor-unit proposals, immutable `DecisionInputV1` captures, exact-payload idempotency, and invoice payment claims shared across mandates within a company generation.
- Database transactions for budgets and reservations, persisted evaluation jobs, execution intents, attempts, and authoritative mock receipts. Semantic evaluation runs outside financial transactions. PostgreSQL serializes short financial writes on the workspace row; SQLite uses database write transactions for local compatibility.
- Assigned reviews with optimistic revisions, distinct eligible approvers, optional separation of duties, comments, and evidence/authority/policy/approval-rule bindings. Current eligibility and financial authority are checked again before dispatch.
- A persisted fixture runner with stable run/tool-call request IDs and stored outcomes. It waits across restarts and resumes after a review resolves. A finished run does not imply every invoice was paid.
- Scoped REST and official-SDK local stdio MCP clients. Agent responses omit internal snapshots, bank records, review internals, and downstream credentials.
- A fixture Evaluation Lab, immutable experiment manifests, partial/cancelled run inspection, three-class metrics, and asynchronous shadow comparisons using the same canonical evaluator. Candidate financial controls apply equally to its hard-only, general-judge, and typed-policy comparison arms.
- Alembic migrations, explicit read-only legacy archive import, Docker/Compose artifacts, liveness/readiness probes, structured operational events, and payload-free OpenTelemetry API span hooks.

### Start the workspace application

Install the exact tested dependency set and migrate the new database:

```sh
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m tollgate.platform_server --embedded-worker
```

Open the private workspace URL printed by the launcher. `--embedded-worker` is a single-process, local-only convenience; jobs and checkpoints still live in the database. The browser removes the credential fragment and keeps this application's token in memory. Reloading requires the private link or sign-in again. The original Control Room/Studio continue to use their existing tab-session credential storage.

For separate API and worker processes, omit `--embedded-worker` and run this in another terminal:

```sh
.venv/bin/python -m tollgate.jobs.worker
```

Both launchers and the worker load documented `.env` settings; exported environment values win. Alembic reads only `DATABASE_URL` from `.env`. The default workspace database is `.runtime/platform.sqlite`; the original launcher still uses `.runtime/control.sqlite`. No migration automatically copies the latter. SQLite is a local workstation option: use one API and one worker, or the embedded worker. Hosted and multi-worker deployments require PostgreSQL.

The original `python -m tollgate.demo_server` launcher also installs the workspace routes in local mode, migrates the separate workspace database, and prints its workspace URL. Start its workspace worker separately. Its existing optional live-provider paths remain local prototype functionality, separate from the new workspace capability gates.

### Offline acceptance walkthrough

1. Create a synthetic workspace, then authorize a mandate with selected invoices, a budget, an automatic limit, and a pinned policy. Defaults are $60,000 and $10,000. Existing beneficiary destinations are pinned server-side.
2. Run the fixture agent. The complete six-invoice batch produces three confirmed mock payments, one delivery-dispute denial, and two assigned reviews: missing receiving confirmation and a payment above the automatic limit.
3. Inspect a proposal to see deterministic checks, typed fixture answers, captured inputs, submitting identity, and its receipt. A queued proposal has not executed.
4. On `INV-7002`, append a receiving assertion such as “Receiving confirms complete delivery for INV-7002; all goods accepted.” The previous review becomes stale and releases its reservation. Explicitly reassess the old proposal to evaluate the new evidence and obtain a fresh result. These receiving records are operator assertions, not independently authenticated external documents.
5. In Connections, issue a scoped credential for the mandate. Save the displayed configuration once; closing its dialog clears the displayed secret. Run the external REST example or MCP client below.
6. In the Lab, save an immutable candidate with `require_confirmation: false` and a distinct name. Compare it with the original version on the public synthetic development split. Enable it as a shadow candidate before submitting a fresh missing-confirmation proposal in a fresh synthetic generation: the active policy reviews it while the candidate allows it. The shadow result cannot execute or alter its reservation.
7. Approve an eligible assigned review, or reject it to release its reservation. Evidence changes, revoked/expired authority, duplicate claims, stale revisions, or ineligible approvers prevent dispatch. Multi-person rules require separately authenticated eligible people; multiple clicks from one actor cannot satisfy them.

Synthetic reset is available through `POST /api/v1/workspaces/{wid}/reset` with `expected_revision`. It creates a new company generation, revokes old mandates/credentials, cancels undispatched work, retains history, and preserves usage counters. It is refused while dispatched/submitted/unknown intents need reconciliation. A fresh mandate in the **same** generation cannot pay an already claimed invoice again; a deliberately reset generation is a new synthetic company. Limits include three workspaces per identity, 500 total workspaces, five resets, 500 proposals per workspace, 20 runs/experiments, 20-job admission and concurrency ceilings in each workspace operational/evaluation pool, 100 concurrent jobs globally, five shadow subscriptions, 100 policy versions, and 12 evidence items per invoice per generation. Accepted work may enqueue follow-up execution/checkpoint jobs beyond its admission queue ceiling so a full queue cannot deadlock its outbox; concurrent running work stays database-bounded. Configurable ceilings are documented in `.env.example`.

### External agents

Use only the issued agent credential, never the local operator token:

```sh
export TOLLGATE_BASE_URL=http://127.0.0.1:8765
export TOLLGATE_WORKSPACE_ID='workspace ID from Connections'
export TOLLGATE_MANDATE_ID='mandate ID from Connections'
export TOLLGATE_AGENT_TOKEN='issued scoped agent secret'
export TOLLGATE_RUN_KEY='a caller-owned stable run identifier'
.venv/bin/python -m examples.scoped_agent
```

`tollgate.client.TollgateClient` provides `submit`, `get`, `wait`, approved resource reads, and receipt reads. `wait` returns structured review/deny/unknown outcomes; a polling timeout or cancellation does not cancel accepted financial work. Payment dates are ISO calendar dates checked against the server’s UTC day, matching the original gateway. The example derives its date in UTC; browser timestamps are displayed in the browser’s local timezone. Persist proposal IDs and reuse the exact request ID and payload after transport failures. Changing an amount, destination, claims, or other submitted fields under an existing request ID returns conflict.

For an independently hosted agent's MCP configuration, use the one-time Connections configuration or launch:

```sh
.venv/bin/python -m tollgate.integrations.mcp
```

It uses the official MCP SDK over stdio and the same scoped REST service. Its catalog contains only approved invoice/vendor reads, payment proposals, proposal status, and authorized receipts. Pending review does not execute; agents can continue other eligible invoices and poll after reconnecting. Hosted Streamable HTTP OAuth/resource-audience integration is **not implemented**.

Credential revocation immediately blocks new submissions and polling with that credential. It leaves accepted jobs intact. Mandate revocation additionally cancels undispatched work and pending reviews. Dispatch is a committed boundary: an already dispatched operation follows reconciliation and retains its claim until its outcome is known. The mock executor safely reproduces the same logical result using its stable intent key. No exactly-once guarantee is made for remote effects, and no remote financial executor is connected.

### Lab reproducibility and limits

```sh
.venv/bin/python -m evals.gateway_eval --split development
.venv/bin/python -m evals.gateway_eval --split held_out --output .runtime/gateway-held-out-fixtures.json
```

These commands have no executor/database capability and make zero paid calls. They write ignored local reports. The versioned manifest is `evals/data/current_gateway_v1.json`: 120 distinct synthetic inputs, 24 grouped families, an 80/40 split, and at least ten provisional examples of each expected class per split. Labels and adjudication metadata are excluded from provider inputs. All labels are **unreviewed**; the public synthetic held-out split is not a hidden, independently adjudicated acceptance set.

The fixture backend has known language limitations, including contradictory and partial receiving cases. Its failures remain visible. Fixture results do not establish live Jev performance, document authenticity, or production safety. The general LLM arm is explicitly unavailable, rather than replaced with a fabricated prediction. Operational fail-closed holds can match a desired workflow outcome; they are excluded from available-answer semantic accuracy. Metrics include confusion matrices, per-label unsafe allowances, intervention rates, availability, subgroup denominators, family ranges, and observed timing. Pricing remains unavailable. Fixture timing cannot establish provider latency or cost.

Every experiment freezes its inputs, policies, compiler/contracts, split/case hashes, and arm configuration. Experiments can be cancelled, retain partial rows, and expose authorized JSON results. Shadows capture the pre-action input and use a restricted evaluation repository interface; this is an application capability boundary, not a claim of a separate PostgreSQL database role. Shadow quota exhaustion cannot roll back an operational verdict. Deeper Lab results do not silently replace the four-case development admission used for offline policy activation.

The shared Jev full-response adapter validates exact pinned model identity, response/answer shapes, expected question IDs/types, finite bounds, distribution labels and sums, Choice maximum consistency, Score weighted-mean consistency, supplied legends, and nonnegative integer token usage. Distribution/Choice tolerance is 0.02; Score tolerance is 0.02 times the number of levels. Missing legends remain compatible with older responses; supplied legends must match. Confidence is bounded without assuming an undocumented formula. Fractional Scores and the legacy client adapter are preserved. See the provider's [Score contract](https://docs.typesafe.ai/primitives/score) and [Choice contract](https://docs.typesafe.ai/primitives/choice).

### Containers, migrations, and recovery

`compose.yaml` supplies PostgreSQL, migrations, API, and a separate worker. It binds the API to host loopback only, uses a synthetic local database password, and requires a private operator token:

```sh
export TOLLGATE_OPERATOR_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose build api
docker compose up -d --no-build
```

Open `http://127.0.0.1:8765/workspace` and enter that token. Use `docker compose logs worker` for safe error codes and `GET /health/ready` to check schema and worker heartbeat. `/health/live` does not query providers. Authorized `/api/v1/workspaces/{wid}/operations` exposes job states, queue age, unknown executions, and usage counters without evidence or credentials. SIGTERM stops workers from claiming new work and lets the current bounded handler finish. Expired leases recover accepted work after a crash. Expired authority cancels undispatched reviews and releases their reservations; dispatched or unknown results retain their claims. Repeated crashes that exhaust evaluation attempts hold the proposal, and exhausted dispatched attempts become unknown rather than being retried blindly. Failed jobs expose safe error codes; exhausted evaluations hold proposals, and partial experiments remain inspectable.

Before changing schemas: stop new submissions, drain/stop workers, back up the database, run `alembic upgrade head`, then restart workers and API. Migrations are versioned through `0004_model_usage`. The legacy source database is never modified. Explicit archive import is dry-run first:

```sh
.venv/bin/python -m tollgate.storage.legacy_import .runtime/control.sqlite WORKSPACE_ID
.venv/bin/python -m tollgate.storage.legacy_import .runtime/control.sqlite WORKSPACE_ID --apply
```

The importer reconciles receipt amounts/counts, preserves source IDs and exact source JSON hashes/history, and skips identical archives on retry. Archives are read-only and do not invent canonical financial state, executable mandates, or resumable reviews. Back up SQLite with its backup API while writes are stopped, rather than copying a live database file. For the local Compose database:

```sh
mkdir -p .runtime
docker compose exec -T postgres pg_dump -U tollgate -Fc tollgate > .runtime/tollgate-backup.dump
```

Test a restore into a new empty database using `pg_restore` and the matching application/schema version before replacing service state. Keep backups private. Roll back application code only with a compatible schema; otherwise restore the matching backup during downtime. Never truncate longer experiment identities or financial history to force a downgrade. Review restored credentials, leases, reservations, and unknown intents before accepting submissions. Restoring a database is not proof that an external operation failed. Unknown/submitted intents must be reconciled by stable provider operation/idempotency identity before claims can be released; do not blindly retry a remote write. Only the mock executor is currently wired.

### Verification and remaining work

The implementation has exercised SQLite fixture acceptance, independent PostgreSQL process races, an independent official-SDK MCP handshake through a real local HTTP listener, and local browser flows. The exact verification record and milestone limitations are in `docs/platform-implementation.json`.

Run the complete offline suite and JavaScript checks:

```sh
.venv/bin/python -m pytest
node --check dashboard/control.js
node --check dashboard/studio.js
node --check dashboard/workspace.js
node --check dashboard/api.js
node --check dashboard/auth.js
node --check dashboard/shell.js
```

Optional tests use disposable local infrastructure, never paid models:

```sh
docker run -d --rm --name tollgate-postgres-test \
  -e POSTGRES_PASSWORD=tollgate-test -e POSTGRES_DB=tollgate_test \
  -p 127.0.0.1:55432:5432 postgres:17
TOLLGATE_TEST_POSTGRES_URL='postgresql+psycopg://postgres:tollgate-test@127.0.0.1:55432/tollgate_test' \
  .venv/bin/python -m pytest tests/test_postgres.py
TOLLGATE_RUN_MCP_INTEGRATION=1 .venv/bin/python -m pytest tests/test_mcp_integration.py
```

PostgreSQL tests create and remove only random test schemas. MCP integration starts a temporary loopback HTTP server and independent stdio process. Tests without these flags skip those integration suites explicitly.

Outstanding release requirements are substantial: provider-backed browser sign-in/refresh and hosted revocation verification, hosted MCP OAuth, reviewed semantic admission and publicly configured provider-backed comparison arms, live-runtime wiring of the model-allowance service, full OTLP export/context propagation, private retention cleanup, scoped Studio draft migration, richer review assignment UI, Jev claim verification, QuickBooks sandbox OAuth/import/writeback/reconciliation, and the read-only vision inspector. Those integrations are not represented as working adapters. The JWT adapter verifies issuer/audience/signature/expiry/allowed algorithms using PyJWT and checks database membership on requests, but provider session logout/revocation behavior has not been verified. Organization onboarding and public hosting are not deployed.

New platform live Jev/live agent/enforcing-claims/vision flags fail startup when enabled; credentials alone cannot enable paid workspace execution. The model-allowance service now enforces durable call/token reservations for the canonical general-judge adapter. Paid provider selection is still not exposed by the platform; the original local prototype provider paths are not metered by this service. Claims currently reject rather than being silently ignored. Accounting reports unavailable. The existing static Site was not changed. There were no paid provider calls, external accounting operations, or public deployments in this implementation session.


### Model-usage accounting and the canonical general judge

`ModelUsage` now reserves a model attempt before dispatch, under database counter locks. It records a stable logical key, bounded attempt number, scoped resource, exact request/configuration hash, requested/resolved model identity, token allowance, normalized response, and append-only usage transitions. An identical retry retrieves the same attempt; changed request/configuration fields conflict. The SDK's own retries are disabled so a retry cannot bypass reservation accounting.

Global and workspace totals include charged and reserved capacity. Evaluation/shadow usage also has a sub-budget: `TOLLGATE_EVALUATION_USAGE_FRACTION` defaults to `0.2` and cannot exceed `0.5`. This leaves capacity for operational work. Synthetic reset does not clear counters and now explicitly cancels queued/running experiments. Cancellation is rechecked under the workspace lock immediately before committing the dispatch marker, so a cancellation between reservation and dispatch releases unused allowance. An already dispatched request can finish and record its usage, while subsequent experiment rows stop.

Undispatched expired reservations are released. Dispatched failures with unknown token usage consume their full allowance; the original attempt cannot be sent again. A bounded retry needs a distinct attempt and fresh allowance. Known provider usage reconciles the reservation, including cache-read/cache-creation input tokens. A late authoritative response can reconcile a conservative unknown charge. The allowance is an application reservation, not a provider-enforced token ceiling or a dollar price: configuring an undersized allowance cannot undo an already sent request. If reported usage exceeds it, the overrun is charged, remains inspectable, and the comparison holds instead of accepting an allow result. Verified input bounds and provider/account acceptance are still required before live enablement.

The new `GeneralJudgeClient` uses the installed Anthropic SDK's structured-output interface with a strict three-class semantic schema. It receives the same server-captured state, compiled question meanings, and pinned semantic rules; it does not use the legacy `Action` benchmark input. Hard checks remain shared and dominate its output. Refusals, timeouts, model mismatch, malformed/extra output fields, invalid usage, and attempts to deny solely on review-only roles have explicit unavailable outcomes. Requests and outputs preserve the actual model and transport provenance; error bodies and credentials are not stored in operational logs.

`BudgetedGeneralJudge` wires this adapter to the usage service. Server-owned local owner-verification experiments may freeze an explicit `GeneralJudgeConfig` and receive a matching injected driver. Missing or changed runtime bindings stay unavailable, with no fixture fallback. Completed experiment rows are reused on worker restart. A saved response is also reused if the worker stopped before saving its result row, then recomposed with the current hard checks. Cases first reached on a later worker delivery start their own first model attempt. Experiment dispatch totals come from durable attempt records, even when a row reuses a completed answer. The Lab displays recorded dispatch attempts rather than assuming every run made zero calls.

This binding is currently a developer/contract-testing interface, not a new browser or agent-controlled live switch. Default REST-created experiments still show the general-LLM arm as unavailable. No model account, supported account model list, or paid integration has been verified. The existing live-execution flags remain blocked until admission and the remaining hosted controls are implemented.

Authorized human views are available at `GET /api/v1/workspaces/{wid}/model-usage` and `/model-usage/{request_id}`. The first returns workspace totals/sub-budget capacity; the second provides the exact scoped attempt and transition history. Agents cannot read these views. The general-judge adapter and restart/cancellation contracts are tested with an injected SDK stand-in:

```sh
.venv/bin/python -m pytest tests/test_model_usage.py
```

Migration `0004_model_usage` preserves existing counters and adds reserved capacity and attempt history. A downgrade that would delete usage history is rejected; use a compatible backup/application version instead. Official interface references: [Claude structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) and [Messages API](https://platform.claude.com/docs/en/api/messages/create). These references support the adapter contract, not live integration results.
