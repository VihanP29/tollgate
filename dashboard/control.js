"use strict";
const $ = id => document.getElementById(id);
const money = cents => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: cents % 100 ? 2 : 0 }).format(cents / 100);
const state = { token: sessionStorage.getItem("tollgate-operator") || "", session: null, selected: null, config: null, showReads: false, busy: false, polling: false, eventSignature: "", ledgerSignature: "", inspectorSignature: "" };
let noticeTimer;
function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = "notice" + (error ? " error" : "");
  $("notice").hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 5500);
}
async function api(path, options = {}) {
  const headers = { Authorization: "Bearer " + state.token, ...(options.body ? { "Content-Type": "application/json" } : {}) };
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    sessionStorage.removeItem("tollgate-operator");
    if (!$("login-dialog").open) $("login-dialog").showModal();
    throw new Error("Operator access required. Use the private server link.");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : "Request could not be completed");
  }
  return response.json();
}
function post(path, body = {}) { return api(path, { method: "POST", body: JSON.stringify(body) }); }
function sessionPath() { return "/api/sessions/" + state.session.session_id; }
function timeLabel(value) { return new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function connection(ok) {
  $("connection-dot").classList.toggle("online", ok);
  $("connection-text").textContent = ok ? "Connected" : "Reconnecting";
}
function meaningfulEvents() {
  return (state.session?.events || []).filter(e => state.showReads || !["read_invoice", "lookup_vendor"].includes(e.tool));
}
function eventTitle(event) {
  const vendor = state.session.vendors[event.args.vendor_id]?.name || event.args.vendor_id || "";
  if (event.tool === "schedule_payment") return vendor + " · " + money(Math.round(Number(event.args.amount_usd) * 100));
  if (event.tool === "update_vendor") return "Change bank details · " + vendor;
  if (event.tool === "read_invoice") return "Read " + event.args.invoice_id;
  if (event.tool === "lookup_vendor") return "Look up " + vendor;
  return event.tool.replaceAll("_", " ");
}
function executionLabel(event) {
  if (event.resolution === "approve") return "APPROVED & PAID";
  if (event.execution === "rejected") return "REJECTED";
  if (event.execution === "cancelled") return "CANCELLED";
  return { allow: "ALLOWED", escalate: "IN REVIEW", deny: "DENIED" }[event.verdict];
}
function update() {
  const s = state.session;
  if (!s) return;
  const mandate = s.mandate;

  $("task-rule").textContent = s.policy?.require_confirmation ? "Hold disputes · require eligible receiving confirmation" : "Hold unresolved delivery disputes";
  $("paid").textContent = money(s.spent_cents);
  $("paid-count").textContent = s.ledger.length + (s.ledger.length === 1 ? " payment" : " payments");
  $("reserved").textContent = money(s.reserved_cents);
  $("pending-count").textContent = s.events.filter(e => e.execution === "pending").length + " pending";
  $("blocked").textContent = s.events.filter(e => e.verdict === "deny").length;
  $("remaining").textContent = money(s.remaining_cents);
  $("mandate-budget").textContent = money(mandate.budget_cents);
  $("mandate-auto").textContent = money(mandate.auto_limit_cents);
  $("mandate-expiry").textContent = timeLabel(mandate.expires_at);
  $("mandate-id").textContent = mandate.mandate_id + " · policy " + (s.policy?.id || "legacy");
  const inactive = mandate.revoked || new Date(mandate.expires_at) <= new Date();
  $("mandate-status").textContent = mandate.revoked ? "REVOKED" : inactive ? "EXPIRED" : "ACTIVE";
  $("mandate-status").classList.toggle("revoked", inactive);
  $("revoke").disabled = inactive;
  $("new-mandate").disabled = s.status === "running" || state.busy;
  $("recovery").hidden = !s.recovery_message;
  $("recovery-message").textContent = s.recovery_message || "";
  const outcomes = s.invoice_outcomes || {};
  $("invoice-outcomes").textContent = `${outcomes.paid || 0} paid · ${outcomes.awaiting_review || 0} awaiting review · ${outcomes.blocked || 0} blocked · ${outcomes.unprocessed || 0} unprocessed invoices`;
  $("challenge").disabled = !s.runner_available || ["ready", "running"].includes(s.status) || inactive || state.busy;
  $("run-agent").disabled = !s.runner_available || s.status !== "ready" || inactive || state.busy;
  $("planner").disabled = s.status !== "ready";
  $("run-agent").replaceChildren(node("span", "", s.status === "running" ? "◌" : "▶"), document.createTextNode(s.status === "running" ? "Agent running" : "Run agent"));
  const mode = s.planner === "claude" ? "Live Claude agent" : "Scripted rehearsal";
  $("run-state").textContent = s.status === "ready" ? "Scoped credential issued · ready" : mode + " · " + (["finished", "completed"].includes(s.status) ? "agent finished" : s.status.replaceAll("_", " "));
  $("activity-count").textContent = meaningfulEvents().length;
  renderStream(); renderLedger(); renderInspector();
}
function renderStream() {
  const events = meaningfulEvents();
  const signature = JSON.stringify([events, state.selected, state.session.error, state.session.final_text]);
  if (signature === state.eventSignature) return;
  const stream = $("stream");
  const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 55;
  const first = state.eventSignature === "";
  state.eventSignature = signature;
  if (!events.length && !state.session.error) {
    if (stream.querySelector(".empty")) return;
    const empty = node("div", "empty");
    empty.append(node("span", "empty-symbol", "↗"), node("h3", "", "Ready to act, within limits."), node("p", "", "Run the agent to watch each financial action pass through its authorization checks."));
    stream.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  if (state.session.error) fragment.append(node("div", "run-error", state.session.error));
  for (const event of events) {
    const row = node("button", "event " + event.verdict + (state.selected === event.event_id ? " selected" : ""));
    row.type = "button";
    row.setAttribute("aria-label", eventTitle(event) + ": " + executionLabel(event));
    const symbol = node("span", "event-symbol", event.verdict === "deny" ? "×" : event.verdict === "escalate" ? "Ⅱ" : "✓");
    const main = node("div", "event-main");
    const top = node("div", "event-top");
    top.append(node("span", "event-title", eventTitle(event)), node("span", "verdict-pill", executionLabel(event)));
    const source = event.origin === "adversarial_probe" ? "PROBE · " : "";
    main.append(top, node("div", "event-sub", source + (event.args.memo || event.args.invoice_id || event.tool) + "  /  " + event.latency_ms.toFixed(1) + " ms"), node("div", "event-reason", event.reasons.join(" · ")));
    row.append(symbol, main);
    row.onclick = () => { state.selected = event.event_id; renderStream(); renderInspector(); };
    fragment.append(row);
  }
  if (state.session.final_text) {
    const summary = details("Agent summary · execution receipts are authoritative", state.session.final_text);
    summary.className = "agent-summary";
    fragment.append(summary);
  }
  stream.replaceChildren(fragment);
  if (atBottom || first) stream.scrollTop = stream.scrollHeight;
}
function renderLedger() {
  const ledger = state.session.ledger;
  const signature = JSON.stringify(ledger);
  if (signature === state.ledgerSignature) return;
  state.ledgerSignature = signature;
  $("ledger-count").textContent = ledger.length;
  if (!ledger.length) {
    $("ledger").replaceChildren(node("div", "ledger-empty", "No payments executed yet. Blocked and pending actions cannot reach the ledger."));
    return;
  }
  const table = node("table");
  const head = node("thead"), row = node("tr");
  ["RECEIPT", "BENEFICIARY", "INVOICE", "DESTINATION", "AMOUNT", "STATUS"].forEach((title, i) => row.append(node("th", i === 4 ? "amount" : "", title)));
  head.append(row);
  const body = node("tbody");
  for (const payment of ledger) {
    const r = node("tr");
    r.append(node("td", "mono", payment.payment_id), node("td", "", payment.vendor), node("td", "mono", payment.invoice_id), node("td", "mono", "•••• " + payment.bank_account.slice(-4)), node("td", "amount", money(payment.amount_cents)), node("td", "receipt-status", "EXECUTED ✓"));
    body.append(r);
  }
  table.append(head, body);
  $("ledger").replaceChildren(table);
}
function details(title, text) {
  const el = node("details");
  el.append(node("summary", "", title), node("pre", "", text));
  return el;
}
function renderInspector() {
  const queueFilter = $("queue-filter").value;
  const pending = state.session.events.filter(e => e.execution === "pending" && (!queueFilter || (e.semantic_evaluation?.queue || "General review") === queueFilter));
  const event = state.session.events.find(e => e.event_id === state.selected);
  const signature = JSON.stringify([pending, event, state.busy]);
  if (signature === state.inspectorSignature) return;
  state.inspectorSignature = signature;
  const queue = document.createDocumentFragment();
  $("inspector-title").textContent = event ? "Decision evidence" : "Human review";
  for (const item of (!event ? pending : event.execution === "pending" ? pending.filter(p => p.event_id === event.event_id) : [])) {
    const card = node("div", "review-card");
    card.append(node("strong", "", eventTitle(item)), node("small", "", item.semantic_evaluation?.queue || "General review"), node("p", "", item.reasons.join(" · ")));
    const inspect = node("button", "text-button", "Inspect this decision →");
    inspect.onclick = () => { state.selected = item.event_id; renderStream(); renderInspector(); };
    card.append(inspect);
    const actions = node("div", "review-actions");
    for (const choice of ["approve", "reject"]) {
      const button = node("button", "button " + (choice === "approve" ? "primary" : "secondary"), choice === "approve" ? "Approve & execute" : "Reject");
      button.disabled = state.busy;
      button.onclick = () => resolve(item.event_id, choice);
      actions.append(button);
    }
    card.append(actions);
    queue.append(card);
  }
  $("review-queue").replaceChildren(queue);
  if (!event) {
    if (!$("inspector").querySelector(".inspector-empty")) {
      const empty = node("div", "inspector-empty");
      empty.append(node("div", "shield", "✓"), node("h3", "", "Every decision has a record."), node("p", "", "Select an action to inspect its checks, supporting evidence, and execution result."));
      $("inspector").replaceChildren(empty);
    }
    return;
  }
  const fragment = document.createDocumentFragment();
  if (state.session.policy && event.tool === "schedule_payment") {
    const controls = node("div", "review-actions");
    const add = node("button", "button secondary", "Add evidence"); add.onclick = () => openEvidence(event);
    const again = node("button", "button primary", "Re-evaluate payment");
    again.disabled = state.busy || event.execution === "executed" || state.session.mandate.revoked;
    again.onclick = async () => {
      state.busy = true; renderInspector();
      try { const result = await post(sessionPath()+"/events/"+event.event_id+"/reassess"); state.selected=result.event_id; await refresh(); notice("Fresh proposal: " + result.execution); }
      catch(error) { notice(error.message,true); } finally {state.busy=false;renderInspector();}
    };
    controls.append(add,again); fragment.append(controls);
    const timeline=state.session.evidence[event.args.memo] || [];
    fragment.append(details("Evidence timeline · "+timeline.length+" records",timeline.map(r=>r.evidence_id+" · "+r.source_type+"\n"+r.body+(r.supersedes_id ? "\nCorrects "+r.supersedes_id : "")).join("\n\n") || "No receiving records supplied."));
  }
  if (event.semantic_evaluation) {
    const a=event.semantic_evaluation.answers;
    fragment.append(node("div","typed-summary", "JEV · 3 probability checks + Choice + Score\n" + a.exception_category.choice + " · confidence " + a.exception_category.confidence.toFixed(2) + "\nDelivery confirmation " + a.delivery_confirmation.score.toFixed(2) + " / 3 · " + event.semantic_evaluation.queue));
  }
  const head = node("div", "evidence-head");
  head.append(node("span", "", eventTitle(event)));
  fragment.append(head, node("div", "evidence-meta", event.event_id + " · " + timeLabel(event.created_at) + " · " + (event.origin || "external_agent").replaceAll("_", " ") + "\nReviewer: " + event.reviewer));
  const message = event.execution === "executed" ? (event.resolution === "approve" ? "Operator approved. Limits were rechecked and the payment executed once." : "Authorization passed. The executor completed this action.") : event.execution === "pending" ? "Not executed. Budget is reserved while awaiting operator review." : event.execution === "cancelled" ? "Cancelled. Authorization was revoked or supporting state changed." : event.execution === "rejected" ? "Rejected by the operator. No payment was executed." : "Blocked before execution. No financial state was changed.";
  fragment.append(node("div", "execution-banner " + event.execution, message));
  const passed = event.checks.filter(c => c.kind !== "semantic" && c.outcome === "pass");
  if (passed.length) fragment.append(node("div", "passed-summary", "✓ " + passed.length + " authority & financial checks passed"));
  if (event.documents.length && event.checks.some(c => c.kind === "semantic" && c.outcome !== "pass")) {
    const documentText = event.documents.join("\n\n");
    const marker = documentText.toLowerCase().indexOf("supporting correspondence");
    const excerpt = (marker >= 0 ? documentText.slice(marker) : documentText).slice(0, 240);
    const source = node("div", "source-excerpt");
    source.append(node("span", "mini-label", "DOCUMENT EXCERPT · UNTRUSTED"), node("p", "", excerpt + (documentText.length > 240 ? "…" : "")));
    fragment.append(source);
  }
  const visible = event.checks.filter(c => c.kind === "semantic" || c.outcome !== "pass").sort((a,b) => ({fail:0,unavailable:1,review:2,pass:3}[a.outcome] - {fail:0,unavailable:1,review:2,pass:3}[b.outcome]));
  for (const check of visible) {
    const el = node("div", "check " + check.outcome);
    el.append(node("span", "check-kind", check.kind === "semantic" ? (event.reviewer.startsWith("jev-") ? "JEV · SEMANTIC CHECK" : "OFFLINE FIXTURE CHECK") : check.kind === "authority" ? "IDENTITY & AUTHORITY" : "DETERMINISTIC CONSTRAINT"));
    const title = node("div", "check-head");
    const passedSemantic = {instruction_override: "Action stays within operator authority", unresolved_delivery_dispute: "No unresolved delivery dispute detected", conflicting_receiving_evidence: "No conflicting receiving records detected"};
    const label = check.kind === "semantic" && check.outcome === "pass" ? (passedSemantic[check.id] || check.label) : check.label;
    title.append(node("span", "check-icon", check.outcome === "pass" ? "✓" : check.outcome === "fail" ? "×" : "!"), node("span", "", label));
    el.append(title, node("div", "check-detail", check.detail));
    if (check.probability != null) {
      const bar = node("div", "probability");
      const fill = node("i"); fill.style.width = (check.probability * 100) + "%";
      bar.append(fill); el.append(bar);
    }
    fragment.append(el);
  }
  if (event.semantic_evaluation) {
    const evaluation = event.semantic_evaluation;
    fragment.append(node("div", "passed-summary", "Review queue: " + evaluation.queue));
    for (const [key,answer] of Object.entries(evaluation.answers)) {
      if (answer.type === "noul") continue;
      const card=node("div","typed-answer");
      card.append(node("span","mini-label", "JEV · "+answer.type.toUpperCase()),node("h3","",key.replaceAll("_"," ")),
        node("p","",answer.type === "choice" ? answer.choice : "Score "+answer.score.toFixed(2)+" / 3"),node("small","", "Confidence "+answer.confidence.toFixed(2)+" · model signal"));
      for (const [label,probability] of Object.entries(answer.probabilities)) {
        const row=node("div","distribution-row");row.append(node("span","",label+" · "+Math.round(probability*100)+"%"));
        const bar=node("div","probability");const fill=node("i");fill.style.width=(probability*100)+"%";bar.append(fill);row.append(bar);card.append(row);
      }
      if (key === "delivery_confirmation") card.append(details("Score rubric", evaluation.policy.questions.find(q=>q.id===key).criteria.join("\n")));
      fragment.append(card);
    }
    fragment.append(details("Exact Jev evaluation · policy and input",JSON.stringify(evaluation,null,2)));
  }
  if (passed.length) fragment.append(details("Inspect passed authorization checks", passed.map(c => "✓ " + c.label + "\n" + c.detail).join("\n\n")));
  if (event.documents.length) {
    const evidence = details("Supporting evidence · untrusted correspondence", event.documents.join("\n\n"));
    evidence.open = event.checks.some(c => c.kind === "semantic" && c.outcome !== "pass");
    fragment.append(evidence);
  }
  fragment.append(details("Exact tool call", JSON.stringify({ tool: event.tool, args: event.args }, null, 2)));
  if (event.result != null) fragment.append(details("Executor result", typeof event.result === "string" ? event.result : JSON.stringify(event.result, null, 2)));
  $("inspector").replaceChildren(fragment);
}
async function refresh() {
  if (!state.session || state.polling) return;
  state.polling = true;
  const requestedId = state.session.session_id;
  try {
    const result = await api("/api/sessions/" + requestedId);
    if (state.session?.session_id === requestedId) { state.session = result; connection(true); update(); }
  } catch (error) { connection(false); }
  finally { state.polling = false; }
}
async function resolve(eventId, resolution) {
  state.busy = true; renderInspector();
  try {
    const event = await post(sessionPath() + "/review/" + eventId, { resolution });
    notice(event.execution === "executed" ? "Approved. Payment executed once and added to the ledger." : event.execution === "cancelled" ? "Approval cancelled: current authorization or evidence no longer matches." : "Rejected. No payment executed.", event.execution === "cancelled");
    await refresh();
  } catch (error) { notice(error.message, true); }
  finally { state.busy = false; renderInspector(); }
}
async function createMandate(body = {}) {
  const result = await post("/api/sessions", body);
  state.session = result.session; state.selected = null;
  // The agent credential is deliberately not retained in browser storage.
  sessionStorage.setItem("tollgate-session", state.session.session_id);
  state.eventSignature = ""; state.ledgerSignature = ""; state.inspectorSignature = "";
  update();
}
async function connect() {
  state.config = await api("/api/config");
  await loadPolicies();
  const studio = await api("/api/policy-studio");
  $("mandate-policy").value = studio.active_version;
  const activePolicy = studio.versions.find(p=>p.id===studio.active_version);
  if(activePolicy){$("budget-input").value=activePolicy.financial.budget_cents/100;$("auto-input").value=activePolicy.financial.auto_limit_cents/100;}
  connection(true);
  const live = state.config.backend === "jev";
  $("model-pill").textContent = live ? "JEV · LIVE REVIEW" : "OFFLINE · FIXTURE CHECKS";
  $("model-pill").classList.toggle("offline", !live);
  const option = $("planner").querySelector('[value="claude"]');
  option.disabled = !state.config.live_agent_available;
  option.textContent = state.config.live_agent_available ? "Claude · live agent" : "Claude · API key needed";
  const list = document.createDocumentFragment();
  for (const invoice of state.config.invoices) {
    const label = node("label", "invoice-option");
    const input = node("input"); input.type = "checkbox"; input.value = invoice.invoice_id; input.checked = true; input.name = "invoice";
    const name = node("span", "invoice-name", invoice.vendor); name.append(node("small", "", invoice.invoice_id));
    label.append(input, name, node("span", "", money(Math.round(invoice.amount_usd * 100)))); list.append(label);
  }
  $("invoice-options").replaceChildren(list);
  const sessions = await api("/api/sessions");
  const saved = sessionStorage.getItem("tollgate-session");
  const chosen = sessions.find(s => s.session_id === saved) || sessions[0];
  if (chosen) state.session = await api("/api/sessions/" + chosen.session_id);
  else await createMandate();
  $("planner").value = state.session.planner;
  update();
}
$("show-reads").onchange = e => { state.showReads = e.target.checked; state.eventSignature = ""; update(); };
$("new-mandate").onclick = $("recover-mandate").onclick = () => $("mandate-dialog").showModal();
$("challenge").onclick = () => $("challenge-dialog").showModal();
$("close-challenge").onclick = () => $("challenge-dialog").close();
for (const button of document.querySelectorAll("[data-case]")) {
  button.onclick = async () => {
    const buttons = [...document.querySelectorAll("[data-case]")]; buttons.forEach(b => { b.disabled = true; });
    try {
      const event = await post(sessionPath() + "/probe", { case: button.dataset.case });
      state.selected = event.event_id; $("challenge-dialog").close(); await refresh();
      notice("Adversarial probe: " + executionLabel(event).toLowerCase() + ". " + event.reasons.join(" · "));
    } catch (error) { notice(error.message, true); }
    finally { buttons.forEach(b => { b.disabled = false; }); }
  };
}
$("cancel-mandate").onclick = () => $("mandate-dialog").close();
$("mandate-form").onsubmit = async e => {
  e.preventDefault();
  const selected = [...$("invoice-options").querySelectorAll("input:checked")].map(i => i.value);
  if (!selected.length) { notice("Select at least one invoice.", true); return; }
  const button = $("mandate-form").querySelector('[type="submit"]'); button.disabled = true;
  try {
    await createMandate({ budget_usd: Number($("budget-input").value), auto_limit_usd: Number($("auto-input").value), invoice_ids: selected, policy_version_id: $("mandate-policy").value });
    $("mandate-dialog").close(); $("planner").value = "scripted"; notice("Task authorized. A scoped agent credential has been issued.");
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
};
$("run-agent").onclick = async () => {
  if (!state.session || state.busy) return;
  state.busy = true; update();
  try { await post(sessionPath() + "/run", { planner: $("planner").value }); await refresh(); }
  catch (error) { notice(error.message, true); }
  finally { state.busy = false; update(); }
};
$("revoke").onclick = async () => {
  if (!state.session) return;
  try { state.session = await post(sessionPath() + "/revoke"); update(); notice("Authorization revoked. Pending payments cancelled; further actions are blocked."); }
  catch (error) { notice(error.message, true); }
};
$("export").onclick = async () => {
  if (!state.session) return;
  try {
    const payload = await api(sessionPath() + "/audit");
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
    const link = node("a"); link.href = url; link.download = "tollgate-" + state.session.session_id + ".json";
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice("Audit exported with the mandate, checks, decisions and execution receipts.");
  } catch (error) { notice(error.message, true); }
};
$("login-form").onsubmit = async e => {
  e.preventDefault(); state.token = $("token-input").value.trim();
  try { await connect(); sessionStorage.setItem("tollgate-operator", state.token); $("token-input").value = ""; $("login-dialog").close(); }
  catch (error) { notice(error.message, true); }
};
$("login-dialog").addEventListener("cancel", e => e.preventDefault());
const fragmentToken = new URLSearchParams(location.hash.slice(1)).get("token");
if (fragmentToken) {
  state.token = fragmentToken; sessionStorage.setItem("tollgate-operator", fragmentToken);
  history.replaceState(null, "", location.pathname);
}
if (state.token) connect().catch(error => { connection(false); notice(error.message, true); });
else $("login-dialog").showModal();
setInterval(refresh, 700);

let policies = [], replayResult = null, evidenceEvent = null;
async function loadPolicies() {
  policies=await api('/api/policies');
  for (const id of ['mandate-policy','lab-policy']) {
    $(id).replaceChildren(...policies.map(p=>{const option=node('option','',p.name+' · '+p.id.slice(0,6));option.value=p.id;return option;}));
  }
}
function openEvidence(event) {
  evidenceEvent=event;
  $('evidence-body').value='';
  const records=state.session.evidence[event.args.memo] || [];
  const options=[node('option','','No correction')];options[0].value='';
  for(const r of records.filter(r=>r.source_type==='verified_receiving')){const option=node('option','',r.evidence_id);option.value=r.evidence_id;options.push(option);}
  $('evidence-supersedes').replaceChildren(...options);
  $('evidence-dialog').showModal();
}
$('sample-evidence').onclick=()=>{$('evidence-body').value='Receiving team confirms complete delivery for '+evidenceEvent.args.memo+'. All goods were accepted. Earlier delivery issues for this invoice are resolved.';};
$('close-evidence').onclick=()=>$('evidence-dialog').close();
$('evidence-form').onsubmit=async e=>{
  e.preventDefault();const button=e.submitter;button.disabled=true;
  try{await post(sessionPath()+'/invoices/'+evidenceEvent.args.memo+'/evidence',{body:$('evidence-body').value,source_type:$('evidence-source').value,supersedes_id:$('evidence-supersedes').value || null});$('evidence-dialog').close();state.inspectorSignature='';await refresh();notice('Evidence saved. Review the updated record, then explicitly re-evaluate the payment.');}
  catch(error){notice(error.message,true);}finally{button.disabled=false;}
};
function labPolicy(){return policies.find(p=>p.id===$('lab-policy').value);}
function updateLabPolicy(){const p=labPolicy();$('policy-json').value=JSON.stringify(p,null,2);$('lab-review').value=p.review_at;$('lab-deny').value=p.deny_above;}
$('policy-lab').onclick=()=>{updateLabPolicy();$('lab-dialog').showModal();};
$('lab-policy').onchange=updateLabPolicy;
$('close-lab').onclick=()=>$('lab-dialog').close();
async function replay(mode){
  $('threshold-replay').disabled=$('fresh-replay').disabled=true;
  $('lab-results').textContent=mode==='policy' ? 'Evaluating frozen inputs with Jev…' : 'Recomputing from saved answers…';
  try{
    replayResult=await post('/api/replays',{session_id:state.session.session_id,mode,policy_version_id:$('lab-policy').value,review_at:Number($('lab-review').value),deny_above:Number($('lab-deny').value)});
    const result=node('div');result.append(node('p','', (mode==='policy'?'Fresh policy evaluation':'Reused model answers')+' · '+replayResult.model_calls+' model calls · no payments executed'));
    const table=node('table');for(const r of replayResult.rows){const row=node('tr');for(const value of [r.invoice_id,r.original+' → '+r.replay,r.queue])row.append(node('td','',value));table.append(row);}result.append(table);$('lab-results').replaceChildren(result);$('export-replay').disabled=false;
  }catch(error){$('lab-results').textContent=error.message;}finally{$('threshold-replay').disabled=$('fresh-replay').disabled=false;}
}
$('threshold-replay').onclick=()=>replay('threshold');$('fresh-replay').onclick=()=>replay('policy');
$('save-policy').onclick=async()=>{try{const saved=await post('/api/policies',JSON.parse($('policy-json').value));await loadPolicies();$('lab-policy').value=saved.id;updateLabPolicy();notice('Immutable policy saved. Select it when creating a new mandate.');}catch(error){notice(error.message,true);}};
$('export-replay').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(replayResult,null,2)],{type:'application/json'}));const link=node('a');link.href=url;link.download=replayResult.id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

$("queue-filter").onchange=()=>{state.inspectorSignature="";renderInspector();};

$("mandate-policy").addEventListener("change",()=>{const p=policies.find(x=>x.id===$("mandate-policy").value);if(p?.financial){$("budget-input").value=p.financial.budget_cents/100;$("auto-input").value=p.financial.auto_limit_cents/100;}});
