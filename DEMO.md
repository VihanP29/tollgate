> Current recording sequence: [Jev showcase](SHOWCASE.md). The guide below records the earlier two-question demo.

# Tollgate recording guide

The application video should demonstrate the authorization product, not imply access to a real bank. All companies, invoices, balances and execution receipts are synthetic.

## Prepare a take

1. From the final `outputs/Tollgate` folder, double-click `Launch Tollgate.command`. Keep Terminal running. The default port is 8765. Open the private operator URL from Terminal. Keep that credential and Terminal containing it out of the recording.
2. If the server was restarted, use **Create new mandate**; saved sessions cannot rerun the agent or probes. Verify **JEV · LIVE REVIEW**. Choose **Claude · live agent** for a real agent, or explicitly describe **Scripted rehearsal** as a repeatable proposal sequence with live Jev decisions.
3. Use a desktop browser window around 1280×720 or larger. The dashboard has a compact layout for short screens. Record the app area, not API keys or other windows.
4. Click **New mandate** and leave all six invoices selected. Use a $60,000 budget and a $10,000 automatic-payment limit.
5. Start the agent. Confirm the invoice outcome counts; “agent finished” describes the tool loop, while receipts and invoice outcomes show the work actually performed. A live Claude run takes longer than the scripted sequence and can vary. Record a complete run first; keep the screen capture as a backup.

## Recommended 90-second sequence

| Time | Screen | Narration |
|---|---|---|
| 0–15 s | Mandate form | “I built Tollgate because financial agents need explicit authority before they can safely act. A prompt telling an agent to be careful doesn't enforce what it can do.” |
| 15–30 s | Authorize the task; show the task budget and pinned beneficiaries | “Here I authorize one payment task: selected invoices, existing bank destinations, a total budget, and a limit above which a human must approve.” |
| 30–45 s | Real Claude run or prepared capture of it | “This is a real Claude agent using tools. Every proposal goes through the same gateway. Code checks permissions and financial facts; Jev classifies narrow questions about conflicting document instructions.” |
| 45–65 s | Challenge gate → Ignore a delivery dispute | “Now I submit a labeled adversarial proposal. The amount, beneficiary and budget all pass, but the receiving correspondence says the delivery is disputed. Jev catches that semantic conflict and the executor refuses the payment.” |
| 65–80 s | Select the pending $38,000 payment → Approve & execute; inspect its receipt | “This legitimate large payment needs approval. Approving it rechecks current authority and executes it once. Retries can't pay the invoice twice.” |
| 80–90 s | Ledger and evidence panel | “On sixteen synthetic semantic test cases, repeated twice, it stopped the risky payments and allowed the legitimate ones. This is a prototype; my next step is testing it in shadow mode on real financial workflows.” |

Read naturally and shorten the opening if necessary. The exact runtime depends on speaking speed. Keep the live run outside the narration's critical path by recording it in advance and cutting to its result.

## Two more shots worth capturing

- **Challenge gate → Redirect a payment:** the bank edit is denied because it is outside the mandate. This is a deterministic authorization control, not a Jev classification.
- **Challenge gate → Replay a completed invoice:** a new request ID still cannot create another payment. If using the real agent, Acme should already be in the ledger before this probe.

If a live run behaves differently, narrate what actually appears. Claude may ignore the poisoned invoice itself; don't say Tollgate blocked an action Claude never proposed. The explicit probes demonstrate independent enforcement and are labeled **PROBE** in the stream and audit.

## Exact claims supported today

- Live Jev review works with the pinned `jev-1.14.0` client.
- The real Claude tool-using agent completes the six-invoice sandbox task. Its first prompt was refused; explicit sandbox context was added and a successful run was saved.
- There are 16 manually authored semantic acceptance cases, eight benign and eight risky, repeated twice per system. Jev stopped 16/16 risky attempts and auto-executed 16/16 benign attempts in the saved run. The identical hard-controls-only gateway stopped 0/16 of those semantic violations. These cases deliberately pass the hard controls.
- The saved acceptance run's median gateway decision latency is about 83 ms. This is a measurement on this small workload, not a latency guarantee.
- Automated tests cover budgets, pending reservations, exact amounts, destinations, idempotency, parallel approvals, revocation, expiration, stale evidence, classifier outages, API separation and the demo workflow.

## Claims to leave out

- “Eliminates hallucinations,” “proves intent,” “bank-grade,” or “production-ready.”
- “32 independent held-out examples.” There are 16 unique acceptance cases, repeated twice.
- “Cheaper than an LLM judge.” Pricing and a comparable full baseline are not verified for this new gateway.
- The historical 97.7% catch result as the accuracy of the new gateway. It came from the earlier engine and a split with template overlap.
- OpenAI Decisions, image invoices, actual bank integrations or real customers as completed features.

## Recording fallback

If Claude is unavailable, create a fresh mandate, choose **Scripted rehearsal**, and keep **JEV · LIVE REVIEW** enabled. Say: “The proposed action sequence is scripted for reproducibility; every semantic classification is made live by Jev.”

If Jev is unavailable, calls are held for review rather than automatically executed. You can use `--backend offline` to rehearse the UI, but label the recording as an offline fixture demonstration and do not present it as live model evidence.

## Finish before submitting

- Keep one good recording before attempting another take.
- Watch it once with audio: verify the distinction between hard controls, live classification, and labeled probes is clear.
- Show the project limitations and synthetic-data scope in the description or README.
- Submit the application yourself after confirming its current deadline and video requirements in the application form.

## Verification after the polish pass

Live run behavior varies. Narrate the visible counts rather than promising every legitimate invoice will be paid. Generated audit traces are excluded from the source repository.
