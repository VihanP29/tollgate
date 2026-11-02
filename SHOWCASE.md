# Jev showcase: recording and implementation notes

The default new mandate uses **Receiving confirmation required**. It asks three Noul questions, one Score and one Choice together in one live Jev request. Historical mandates retain their earlier behavior; create a new mandate to use the showcase.

## Record this sequence

1. Open the local operator URL. Create a new mandate with the default policy, $60,000 budget and $10,000 automatic limit.
2. Choose Claude for a real agent run. The agent may independently avoid malicious instructions. Use the action stream and invoice outcomes as the execution record.
3. Inspect Hooli's correct $2,240 payment if proposed. Its delivery confirmation is intentionally absent. Jev should hold it and classify the exception as missing confirmation. Show the score, confidence and full probability distributions in the evidence panel.
4. Click **Add evidence**, select the receiving source, then **Insert synthetic complete-delivery example**. Explain that this is a simulated operator-verified receiving record. Save it. The previous review is cancelled; no payment executes on evidence submission.
5. Click **Re-evaluate payment**. This creates a fresh proposal under the same policy with the new evidence. Show the new confirmation score and the execution receipt. The original decision retains the old model input.
6. Open **Policy lab**, choose the default policy and **Reuse answers**. This recomputes outcomes without calling Jev or executing payments.
7. Choose **Dispute controls** and **Evaluate policy with Jev**. Historical proposals are evaluated on their original inputs. Hooli's original hold may become allow under the alternate policy. The real mandate and ledger do not change.
8. Optionally show **Edit bounded semantic policy**. Edit the name, question instructions or thresholds and save a new immutable version. It can be selected for a new mandate; it does not change the current one.

If the real agent skips Hooli, record a fresh take or use the synthetic workflow tests as evidence; do not imply the agent attempted a payment it never proposed. Scripted rehearsal remains available, but deliberately proposes an inflated Hooli amount, which fails deterministic controls and does not demonstrate missing-confirmation resolution. The evidence-resolution story requires a correctly sized payment proposal.

## What was verified

- All 126 automated tests passed, including adapter validation, malformed inputs, persistence failures, stale approvals, source separation, concurrent reassessment, restart, policy versioning and replay isolation.
- A real Claude run paid three routine invoices, held Hooli for missing receiving evidence, and held Vandelay for the monetary approval limit.
- In the browser, a synthetic receiving record cancelled Hooli's stale review. Reassessment changed its confirmation score from 0 to 3 and executed $2,240. The resulting ledger held four payments worth $16,590 and one $38,000 pending review.
- Threshold replay used zero model calls. Fresh policy replay made six model calls and changed the historical Hooli result from review to allow without altering the ledger.
- Restart restored evidence and receipts; retrying reassessment returned the existing receipt without another payment. Operator reassessment does not depend on a cached agent credential.
- The browser reported no warning/error logs during the verified sequence. Screenshots are in `docs/jev-evidence-resolved.png` and `docs/jev-policy-lab.png`; audit and replay records are saved beside them.

## Acceptance evidence

The showcase evaluator can regenerate local results. Its synthetic cases are a demo regression aid, not a representative bank dataset or a calibrated benchmark. Thresholds remain provisional.

Reproduce with explicit paid-call authorization:

```sh
.venv/bin/python -m evals.showcase_eval --live --runs 2 --env-file .env
```

## Design and limits

- The full Jev adapter preserves actual probabilities, confidence, fractional scores, model and usage. The legacy adapter remains compatible with historical evaluations.
- Policy definitions originate in `tollgate/semantic_policy.py`; `policies/control_room.yaml` is a readable snapshot of the shipped presets. Editor versions are stored durably in SQLite and pinned in mandates.
- Score evaluates delivery-confirmation strength. It is not a general fraud or authenticity score. Supplier text cannot establish receiving provenance; provenance is assigned through the authenticated operator path.
- Evidence is append-only, capped at 12 records per invoice and 6,000 characters per addition. Model input beyond the bounded review limit is held instead of silently clipped.
- Replay is synchronous and limited to 30 recorded semantic evaluations. Experiments are stored and exportable; there is no background-job scheduler in this local prototype.
- The bounded editor directly validates and saves an immutable version. Separate draft/publish lifecycle screens were consolidated into this one step.
- Review queues are local filters and classification metadata. They do not send messages or grant different operator permissions.
- Confidence is a model signal, not proof of correctness. Source verification is simulated, the financial executor is mocked, and budgets/duplicates remain isolated to each mandate.

## Policy Studio recording path

Open **Policy Studio** from the Control Room (or `/studio` using the same private operator link). The Studio edits real Jev request configuration and Tollgate enforcement settings.

1. Choose **Dispute controls** as the baseline and clone a draft.
2. In Financial, enable receiving confirmation. In Jev questions, edit shared guidance, instructions, or Noul criteria. Inspect the Choice descriptions and ordered Score rubric. Decision rules show polarity, thresholds, acceptable Score levels and queue mappings.
3. Save the draft and run the required synthetic suite. The live comparison should show missing receiving and supplier-only confirmation moving from Allow to Review. Report actual results if they vary. Inspect typed answers, distributions and exact compiled requests.
4. Save the immutable version, then activate it for new mandates. Create a new mandate in the Control Room and show its pinned policy ID. Previous mandates retain their policy.
5. Use **Saved comparison** to reopen an experiment after a refresh or restart. Export its exact inputs, outputs and rule checks as JSON.

Custom synthetic scenarios can edit receiving evidence and the structured financial snapshot. Comparisons are independent scenarios, not a sequential ledger simulation. Historical events captured before Studio support semantic-only comparison; their financial checks remain frozen. Experiments never execute payments or reserve budget.

The offline backend supports unchanged fixture questions only. Edited prompts require live Jev. It does not simulate comprehension of arbitrary prompt edits.

An optional `TOLLGATE_AUDITOR_TOKEN` environment variable enables a distinct read-only Studio credential. Open `/studio#token=…` with that credential in a separate browser session. API permissions prohibit editing, evaluation, saving and activation. This is local sandbox access, not enterprise SSO.

The initial activation suite contains four synthetic scenarios and is a demo regression gate, not a financial safety certification. Two live runs during implementation matched the eight expected synthetic outcomes. Their generated traces are omitted from the source repository.
