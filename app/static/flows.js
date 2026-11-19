/* /home > Space Config and Flow Config (KH 7.0, K22-K24b), ported from proto/kh-flow-config-proto.html.
     Space Config - project templates (/project-types, the v2 body of K02): scopes, stores, access rules, event actions.
     Flow Config  - flows (/flows, the v2 model of K05) on a canvas: steps with ok / fail exits, Start node, ends.
   Nobody types an id or JSON: values are picked from lists and pickers; a growing list is searched and paged, never a
   dropdown. Uses the page's api(), $, esc(), toast(). */
(function(){
const S = {tab:"space", nodes:{}, blocks:{}, types:{}, tid:null, tdrafts:{}, tsel:null,
           fid:null, draft:null, base:"", meta:null, v1:false, sel:null, test:null, list:[], ltotal:0, lq:"", fcache:{}, scopes:{}, stores:{}, gone:{}, expr:{}, parents:[], warn:[], sprob:[]};
const clone = o => JSON.parse(JSON.stringify(o));
let EVENTS = {}, EV_SOURCES = [];   // GET /flows/events: {id: {id, source, label, desc, hook?, fired}}
const root = () => $(S.tab === "space" ? "scRoot" : "fbRoot");
const slug = s => (s||"").normalize("NFD").replace(/[\u0300-\u036f]/g,"").replace(/đ/g,"d").replace(/Đ/g,"D")
  .toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"").slice(0,40) || "x";
const TABS = {
  space:["Space Config","The template a new space starts from: its scopes, where each keeps its rows and who may read or write them."],
  flow:["Flow Config","What KH runs for an agent, step by step. A flow grants nothing: each step runs with the caller's own rights."]};

// KH 7.0 (K22): Space Config and Flow Config are two /home tabs of their own (#space, #flows); each renders into its
// own view, there is no sub-tab strip. The old Tools list is the flow trigger 'Agent calls it by name' (K24b).
window.loadFlows = async function(tab){
  S.tab = tab ? (tab === "space" ? "space" : "flow") : ($("v-space") && !$("v-space").classList.contains("hide") ? "space" : "flow");
  try {
    if (S.tab === "space") {
      const t = await api("GET", "/project-types");
      S.types = Object.fromEntries(t.types.map(x => [x.id, x]));
      if (!S.tid || !(S.types[S.tid] || S.tdrafts[S.tid])) S.tid = S.types.project ? "project" : Object.keys(S.types)[0];
      if (!Object.keys(EVENTS).length) api("GET", "/flows/events").then(ev => { EVENTS = Object.fromEntries(ev.sources.flatMap(g => g.events.map(e => [e.id, e]))); EV_SOURCES = ev.sources; }).catch(() => {});
      return render();
    }
    const [n, ev] = await Promise.all([api("GET", "/flows/nodes"), api("GET", "/flows/events")]);
    S.nodes = Object.fromEntries(n.nodes.map(x => [x.node, x]));
    S.blocks = Object.fromEntries((n.blocks || []).map(x => [x.b, x])); S.pseudo = n.pseudo || []; S.opsBy = n.ops_by || {};
    EVENTS = Object.fromEntries(ev.sources.flatMap(g => g.events.map(e => [e.id, e]))); EV_SOURCES = ev.sources;
    await flowsPage(true); await refreshWaiting();
    if (S.draft && S.fid) return render();          // coming back to the tab keeps the open draft
    const first = S.fid || (S.list.find(f => f.kind !== "system") || S.list[0] || {}).id;
    if (first) return openFlow(first);
    render();
  } catch(e){ root().innerHTML = '<div class="note e">' + esc(e.message) + '</div>'; }
};

function render(){
  const [h, p] = TABS[S.tab];
  // the other tab's view is emptied, so ids (kcBody, ...) exist once in the page
  const other = $(S.tab === "space" ? "fbRoot" : "scRoot"); if (other) other.innerHTML = "";
  const cv = $("ffCanvas"), keep = cv ? [cv.scrollLeft, cv.scrollTop] : null;   // a redraw keeps where the canvas was scrolled
  root().innerHTML = '<div class="greet"><div><h1>' + h + '</h1><p>' + p + '</p></div></div>'
    + '<div id="kcBody">' + (S.tab === "space" ? spaceHtml() : flowHtml()) + '</div>';
  if (S.tab === "space") wireSpace(); else wireFlow();
  const cv2 = $("ffCanvas"); if (cv2 && keep && !S.resetScroll) { cv2.scrollLeft = keep[0]; cv2.scrollTop = keep[1]; } S.resetScroll = false;
}
function tgl(id, on, label){ return '<span class="kc-tgl' + (on ? " on" : "") + '" id="' + id + '"><i></i>' + label + '</span>'; }

// ═════════════════════════════ SPACE CONFIG ═════════════════════════════
// KH 7.0 (K23): the v2 template body of PUT /project-types/{id} (K02), ported from proto/kh-flow-config-proto.html.
// Access has two levels: the space's rules (dropped on the Access box) apply to every scope; a scope's own rules
// (custom) win over the space rule for the same right. A rule allows ("member.write") or rejects ("!member.write");
// Reject Technical Account (technical.exclude) keeps agents out of one scope, 'Visible to technical accounts' off keeps
// them out of the whole space. Rules only NARROW the caller's token (K03): a template never raises anyone's rights.
const ACCESS = [
  {id:"owner.read", label:"Owner Read", k:"read", d:"the space's owner reads it"}, {id:"owner.write", label:"Owner Write", k:"write", d:"the owner adds and changes rows"},
  {id:"anyone.read", label:"Anyone Read", k:"read", d:"anyone the visibility lets in reads it"}, {id:"anyone.write", label:"Anyone Write", k:"write", d:"anyone the visibility lets in writes it"},
  {id:"member.read", label:"Member Read", k:"read", d:"members of the space read it"}, {id:"member.write", label:"Member Write", k:"write", d:"members add and change rows"},
  {id:"!owner.read", label:"Reject Owner Read", k:"rej", d:"the owner may not read it"}, {id:"!owner.write", label:"Reject Owner Write", k:"rej", d:"the owner may not write it"},
  {id:"!anyone.read", label:"Reject Anyone Read", k:"rej", d:"non-members may not read it"}, {id:"!anyone.write", label:"Reject Anyone Write", k:"rej", d:"non-members may not write it"},
  {id:"!member.read", label:"Reject Member Read", k:"rej", d:"members may not read it"}, {id:"!member.write", label:"Reject Member Write", k:"rej", d:"members may not write it"},
  {id:"technical.exclude", label:"Reject Technical Account", k:"rej", d:"agents (technical accounts) may not touch it"}];
const AL = Object.fromEntries(ACCESS.map(a => [a.id, a]));
const KEY = id => id.replace(/^!/, "");
const isRej = id => id[0] === "!";
const VIS_HINT = {"private":"owner + members only", "semi-public":"signed-in users may read", "public":"anyone may read, writers may write"};
const VIS_ALLOWS = {"private":[], "semi-public":["anyone.read"], "public":["anyone.read","anyone.write"]};
// The scopes of the spec, with the job and the store each starts with. A name outside this list is a custom scope.
const SCOPE_JOB = {about:"what the space is: repos, people, environments", policies:"its mandatory rules", knowledges:"facts and spec chunks",
  findings:"open problems only", "lesson-learns":"solved findings and corrections", logs:"work logs (through the hot log)", events:"what happened, append-only",
  "feature-graph":"business features down to steps", personas:"the agents' roles and how they work", rules:"a person's own rules, tagged",
  "untagged-rules":"rules given without tags yet", relationships:"people and agents one works with"};
const SCOPE_LABEL = {about:"About", policies:"Policies", knowledges:"Knowledges", findings:"Findings", "lesson-learns":"Lesson learns", logs:"Logs", events:"Events",
  "feature-graph":"Feature graph", personas:"Personas", rules:"Rules", "untagged-rules":"Untagged rules", relationships:"Relationships"};
const DEF_STORE = {about:"sqlite", policies:"sqlite", knowledges:"vector", findings:"vector", "lesson-learns":"vector", logs:"event", events:"event",
  "feature-graph":"graph", personas:"sqlite", rules:"sqlite", "untagged-rules":"sqlite", relationships:"graph", "hook-rules":"sqlite", versions:"sqlite", tickets:"sqlite"};
// Scopes whose rows already live somewhere today keep their store (KH answers 422 otherwise).
const FIXED_STORE = ["about", "policies", "knowledges", "findings", "lesson-learns", "logs", "feature-graph", "hook-rules"];
const STORES = [["sqlite", "SQLite", "rows by field"], ["vector", "Vector", "search by meaning"], ["graph", "Kuzu graph", "nodes and links"], ["event", "Event", "append-only, in HANA or SQLite (per install)"]];
const STORE_L = Object.fromEntries(STORES.map(([k, l]) => [k, l]));
// Every space also has these, kept by KH: they take no rules of their own and cannot be removed (spec System scopes).
const SYS_SCOPES = [
  {scope:"members", name:"Members", store:"sqlite", job:"the space's owner and members", write:"the owner and KH maintainers"},
  {scope:"links", name:"Links", store:"graph", job:"one node per row of any scope and the typed links between them", write:"KH, when a row is written"},
  {scope:"index", name:"Index", store:"sqlite", job:"the address book: every key (name, code, path) -> scope + row", write:"KH, when a row is written"},
  {scope:"cases", name:"Cases", store:"vector", job:"case memory: solved chains as pointers to rows, never copies", write:"KH, when an agent finishes a task"}];
const SYS_IDS = SYS_SCOPES.map(s => s.scope);
const slabel = s => SCOPE_LABEL[s.scope] || s.scope;
const storeOf = s => s.store || DEF_STORE[s.scope] || "vector";
// the rule that holds for each right in a scope: the scope's own, else the space's
function ruleFor(d, x, key){ const own = (x.custom || []).find(r => KEY(r) === key); if (own) return own; return (d.access || []).find(r => KEY(r) === key) || null; }
const effAcc = (d, x) => ["owner.read","owner.write","anyone.read","anyone.write","member.read","member.write","technical.exclude"].filter(k => { const r = ruleFor(d, x, k); return r && !isRej(r); });

// The draft of a template is the v2 body; a v1 row (made before KH 7.0) is shown as v2: its per-scope access lists
// become the space rules they share plus each scope's own differences.
function tbody(t){
  const v1 = !Array.isArray(t.access) && (t.scopes || []).some(s => Array.isArray(s.access));
  let access = Array.isArray(t.access) ? t.access.slice() : ["owner.read","owner.write","member.read","member.write"];
  if (v1) { const lists = t.scopes.map(s => Array.isArray(s.access) ? s.access : access); access = access.filter(a => lists.every(l => l.includes(a))); }
  const scopes = (t.scopes || []).filter(s => !s.system && !SYS_IDS.includes(s.scope)).map(s => {
    let custom = Array.isArray(s.custom) ? s.custom.slice() : [];
    if (v1 && Array.isArray(s.access)) { ["owner.read","owner.write","anyone.read","anyone.write","member.read","member.write"].forEach(k => {
      const a = s.access.includes(k), b = access.includes(k); if (a && !b) custom.push(k); if (!a && b) custom.push("!" + k); });
      if (s.access.includes("technical.exclude")) custom.push("technical.exclude"); }
    return {scope:s.scope, job:s.job || "", store:typeof s.store === "string" ? s.store : storeOf(s), custom, events:Array.isArray(s.events) ? clone(s.events) : []}; });
  return clone({version:2, name:t.name, description:t.description || "", visibility:t.visibility || "private", allow_dynamic_scopes:!!t.allow_dynamic_scopes,
    tech_visible:t.tech_visible !== false, access, scopes});
}
function tcur(){ if (!S.tid) return null; if (!S.tdrafts[S.tid]) S.tdrafts[S.tid] = tbody(S.types[S.tid]); return S.tdrafts[S.tid]; }
function tdirty(id){ const d = S.tdrafts[id], t = S.types[id]; if (!d) return false; if (!t) return true; const x = clone(d); delete x._new; return JSON.stringify(x) !== JSON.stringify(tbody(t)); }
const TPAGE = 10;

function spaceHtml(){
  const d = tcur(); if (!d) return '<p class="muted">No template yet.</p>';
  const used = new Set(d.scopes.map(s => s.scope)), t = S.types[S.tid], w = spaceWarnings(d);
  const board = (d.scopes.length ? d.scopes.map((s, i) => scardHtml(d, s, i)).join("") : '<div class="kc-empty">Drag a scope here to start the template</div>')
    + '<div class="kc-hint kc-syshd">System scopes: every space has them, KH keeps them</div>' + SYS_SCOPES.map(sysCardHtml).join("");
  // the template list may grow: a search and 10 per page
  const q = (S.tq || "").trim().toLowerCase(), ids = Object.keys({...S.types, ...S.tdrafts}).filter(id => { const x = S.tdrafts[id] || S.types[id];
    return !q || (id + " " + x.name + " " + (x.description || "")).toLowerCase().includes(q); }), shown = ids.slice(0, S.tmax || TPAGE);
  const list = shown.map(id => { const x = S.tdrafts[id] || S.types[id], made = ((S.types[id]||{}).projects_made||[]).length;
    return '<div class="kc-item' + (id === S.tid ? " on" : "") + '" data-tid="' + esc(id) + '"><b>' + esc(x.name)
      + ((S.types[id]||{}).seeded ? ' <span class="kc-tag">seed</span>' : '') + (!S.types[id] ? ' <span class="kc-tag new">new</span>' : '') + (tdirty(id) ? ' <span class="kc-tag dirty">unsaved</span>' : '')
      + '</b>' + (x.description ? '<small>' + esc(x.description.slice(0, 90)) + '</small>' : '') + '<small>' + (x.scopes || []).filter(s => !s.system).length + ' scopes · ' + esc(x.visibility || "private") + ' · ' + made + ' space' + (made === 1 ? "" : "s") + ' made from it</small></div>'; }).join("")
    + (ids.length > shown.length ? '<button class="btn g s" id="tcMore" style="align-self:flex-start">Show ' + Math.min(TPAGE, ids.length - shown.length) + ' more (' + shown.length + ' of ' + ids.length + ')</button>' : '')
    + (ids.length ? '' : '<p class="kc-hint">No template matches.</p>');
  const scopeChips = Object.keys(SCOPE_JOB).map(k => '<div class="kc-chip two' + (used.has(k) ? " used" : "") + '" data-scope="' + k + '" title="' + esc(SCOPE_JOB[k]) + '"><span class="dot"></span><span class="tx"><b>' + esc(SCOPE_LABEL[k]) + '</b><small>' + esc(SCOPE_JOB[k]) + '</small></span></div>').join("")
    + (d.allow_dynamic_scopes ? '<div class="kc-chip custom two" data-scope="__custom"><span class="tx"><b>+ Custom Scope</b><small>a scope of your own: name it on its card (a-z 0-9 _ -)</small></span></div>'
      : '<p class="kc-hint">Custom scopes: turn on “Allow users to add custom scopes” in the Access box.</p>');
  const accChip = a => '<div class="kc-chip two' + (a.k === "rej" ? " rej" : "") + '" data-acc="' + a.id + '"><span class="tx"><b>' + esc(a.label) + '</b><small>' + esc(a.d) + '</small></span></div>';
  return '<div class="kc-tbar"><span class="ctx">Template <b id="tcCtx">' + esc(d.name) + '</b> <span class="kc-mono">' + esc(S.types[S.tid] ? S.tid : "new") + '</span></span>'
    + '<button class="btn g s" id="tcNew">+ New template</button>' + (t ? '<button class="btn g s" id="tcDel">' + (t.seeded ? "Reset to seed" : "Delete") + '</button>' : '')
    + '<span class="sp"></span><span class="kc-msg" id="tcMsg">' + esc(S.tmsg || "") + '</span><button class="btn g s" id="tcReset"' + (tdirty(S.tid) ? "" : " disabled") + '>Cancel</button><button class="btn s" id="tcSave"' + (tdirty(S.tid) ? "" : " disabled") + '>Save</button></div>'
    + '<section class="kc-sc">'
    + '<div class="kc-col"><div class="kc-name"><label for="tcName">Name</label><input id="tcName" autocomplete="off" value="' + esc(d.name) + '"></div>'
    + '<div class="card kc-panel kc-tpl v-' + esc(d.visibility) + '" id="tcPanel"><div class="kc-access' + (S.tsel === "space" ? " sel" : "") + '" id="tcAccess"><b>Access:</b><select id="tcVis">'
    + Object.keys(VIS_HINT).map(v => '<option value="' + v + '"' + (d.visibility === v ? " selected" : "") + '>' + v[0].toUpperCase() + v.slice(1) + ' · ' + VIS_HINT[v] + '</option>').join("") + '</select>'
    + '<span class="sacc"><span>Whole space:</span>' + spacePills(d) + '</span><span class="dyn">' + tgl("tcDyn", d.allow_dynamic_scopes, "Allow users to add custom scopes") + '<br>'
    + '<span title="Off: technical accounts (agents) do not see this space at all, whatever its scopes allow.">' + tgl("tcTech", d.tech_visible !== false, "Visible to technical accounts") + '</span></span></div>'
    + (w.length ? '<div class="kc-warn" id="tcWarn">' + w.map(esc).join("<br>") + '</div>' : '')
    + '<div class="kc-board" id="tcBoard">' + board + '</div></div></div>'
    + '<div class="kc-col"><h3>Available Scopes <small>drag into the template</small></h3><div class="card kc-panel"><div class="kc-src">' + scopeChips + '</div>'
    + '<p class="kc-hint">Greyed: already in the template. Click or drag a scope to add it.</p></div></div>'
    + '<div class="kc-col"><h3>Available Scope Accessible <small>drag onto the Access box or a scope</small></h3><div class="card kc-panel"><div class="kc-src">'
    + ACCESS.filter(a => a.k !== "rej").map(accChip).join("") + '<div class="kc-hint" style="margin:6px 0 0">Reject</div>' + ACCESS.filter(a => a.k === "rej").map(accChip).join("")
    + '</div><p class="kc-hint">Drop a rule on the Access box: it applies to every scope. Drop it on a scope: custom for that scope, it wins over the space rule for the same right. A Reject takes a right away. Or click the box or a card, then a rule. Who is owner, member or a technical account comes from the sign-in (Keycloak).</p></div></div>'
    + '<div class="kc-col"><h3>Existing Spaces Templates</h3><div class="card kc-panel"><input type="text" class="kc-search" id="tcQ" placeholder="Search ' + Object.keys(S.types).length + ' templates" value="' + esc(S.tq || "") + '"><div class="kc-src">' + list + '</div>'
    + (t && (t.projects_made||[]).length ? '<p class="kc-hint">' + t.projects_made.length + ' space' + (t.projects_made.length === 1 ? " was" : "s were") + ' made from it. They keep their own snapshot.</p>' : '<p class="kc-hint">A new space takes the template as it is at that moment; editing it never changes spaces already made.</p>')
    + '</div></div></section>';
}
function spacePills(d){ const allow = VIS_ALLOWS[d.visibility];
  return (d.access || []).map(a => { const x = AL[a] || {label:a, k:"read"}, wide = a.startsWith("anyone.") && !allow.includes(a);
    return '<span class="kc-ap ' + x.k + (wide ? " wide" : "") + '" title="Applies to every scope that does not set its own rule for ' + esc(KEY(a)) + '">' + esc(x.label) + '<i data-srm="' + a + '">✕</i></span>'; }).join("")
    || '<span class="ph">drop access rules here: they apply to every scope</span>'; }
function sysCardHtml(y){
  return '<div class="kc-scard sys" data-sys="' + y.scope + '" title="System scope: every space has it; KH keeps it"><header><b>' + esc(y.name) + '</b><span class="ctag">system</span></header>'
    + '<div class="kc-hint" style="margin:-2px 0 6px">' + esc(y.job) + '</div>'
    + '<div class="kc-store"><span>Stored in</span><b style="font-weight:600;color:var(--ink)">' + esc(STORE_L[y.store]) + ' · ' + esc(STORES.find(z => z[0] === y.store)[2]) + '</b></div>'
    + '<div class="kc-sysacc"><div><span>Read</span>anyone who can read the space</div><div><span>Write</span>' + esc(y.write) + '</div></div><div class="kc-hint" style="margin-top:4px">kept by KH · takes no rules</div></div>'; }
// Event actions: what KH is to do by itself when something happens in a scope. Stored with the template; not run yet.
const EV_ON = [
  {k:"count_over", l:"Rows over a limit", d:"The scope holds more rows than N; the oldest rows over N are acted on.", p:true, pl:"Limit (rows)", def:100},
  {k:"older_than", l:"Rows older than", d:"A row is older than N days; those rows are acted on.", p:true, pl:"Age (days)", def:28},
  {k:"row_added", l:"A row is added", d:"Each new row, right after it is written."},
  {k:"row_changed", l:"A row is changed", d:"Each row that is updated."}];
const EV_DO = [
  {k:"move_bound", l:"Move to the row's bound space", d:"Each row goes to the space it is bound to (e.g. a log's binding project) and leaves this one."},
  {k:"delete", l:"Delete the rows", d:"The rows are removed for good."},
  {k:"run_flow", l:"Run a flow", d:"A flow runs with the rows as its input items.", p:"flow"}];
const EV_NOTE = "KH runs these in each space made from this template AFTER this save (a space keeps the actions it was made with): the timed ones every few minutes, row added / changed right after the write.";
const evOn = k => EV_ON.find(x => x.k === k) || EV_ON[0], evDo = k => EV_DO.find(x => x.k === k) || EV_DO[0];
function evText(e){ const o = evOn(e.when), a = evDo(e.then);
  return "When " + o.l.toLowerCase() + (o.p ? " " + e.n + (e.when === "older_than" ? " days" : "") : "") + " → " + a.l.toLowerCase() + (e.then === "run_flow" ? " “" + (e.flow || "…") + "”" : "") + (e.dry_run ? " · dry run" : ""); }
function evHtml(s, i){ const l = s.events || [];
  return '<div class="kc-evts">' + l.map((e, j) => '<span class="kc-ev" data-evx="' + i + ':' + j + '" title="Click to change. ' + EV_NOTE + '">⚡ ' + esc(evText(e)) + '<i data-evrm="' + i + ':' + j + '">✕</i></span>').join("")
    + '<button class="kc-evadd" data-evadd="' + i + '">+ Event action</button>' + (l.length ? '<span class="kc-hint">' + EV_NOTE + '</span>' : '') + '</div>'; }
function scardHtml(d, s, i){
  const allow = VIS_ALLOWS[d.visibility], cu = s.custom || [];
  // only the scope's own rules show here; the space's rules show once, in the Access box
  const pills = cu.map(a => { const x = AL[a] || {label:a, k:"read"}, wide = !isRej(a) && a.startsWith("anyone.") && !allow.includes(a), sp = (d.access || []).find(r => KEY(r) === KEY(a));
    return '<span class="kc-ap ' + x.k + ' cust' + (wide ? " wide" : "") + '" title="Custom for this scope' + (sp ? ": wins over the space rule " + (AL[sp] || {}).label : "") + (wide ? ". No effect while the space is " + d.visibility : "") + '">' + esc(x.label) + '<i data-rm="' + a + '">✕</i></span>'; }).join("");
  const std = !!SCOPE_JOB[s.scope], fixed = FIXED_STORE.includes(s.scope);
  return '<div class="kc-scard' + (S.tsel === i ? " sel" : "") + '" data-i="' + i + '"><header><span class="grip">⋮⋮</span>'
    + (std ? '<b>' + esc(slabel(s)) + '</b>' : '<input data-sname value="' + esc(s.scope) + '" placeholder="scope name" maxlength="32">')
    + (cu.length ? '<span class="ctag" title="This scope has its own rules">custom</span>' : '')
    + '<button class="x" data-sdel="' + i + '" title="Remove scope">✕</button></header>'
    + '<input class="job" data-job value="' + esc(s.job || "") + '" placeholder="' + esc(SCOPE_JOB[s.scope] || "what is this scope for?") + '">'
    + '<div class="kc-store" title="' + esc((STORES.find(x => x[0] === storeOf(s)) || [])[2] || "") + '"><span>Stored in</span><select data-store>'
    + STORES.map(([k, l, dsc]) => '<option value="' + k + '"' + (storeOf(s) === k ? " selected" : "") + (fixed && k !== DEF_STORE[s.scope] ? " disabled" : "") + '>' + esc(l) + ' · ' + esc(dsc) + (fixed && k !== DEF_STORE[s.scope] ? " (keeps its store: today's data lives there)" : "") + '</option>').join("") + '</select></div>'
    + '<div class="kc-pills">' + (pills || '<span class="ph">same as the space · drop a rule here to customize</span>') + '</div>'
    + evHtml(s, i) + '</div>';
}
function spaceWarnings(d){
  const out = [], allow = VIS_ALLOWS[d.visibility];
  if (d.tech_visible === false && d.scopes.some(x => effAcc(d, x).includes("technical.exclude"))) out.push("⚠ Reject Technical Account on a scope has no effect: the whole space is hidden from technical accounts.");
  (d.access || []).filter(a => !isRej(a) && a.startsWith("anyone.") && !allow.includes(a)).forEach(a => out.push("⚠ Whole space: " + AL[a].label + " has no effect while the space is " + d.visibility + "."));
  d.scopes.forEach(s => {
    (s.custom || []).filter(a => a.startsWith("anyone.") && !allow.includes(a)).forEach(a => out.push("⚠ " + slabel(s) + ": " + AL[a].label + " has no effect while the space is " + d.visibility + "."));
    if (!effAcc(d, s).some(a => a.endsWith(".write"))) out.push("⚠ " + slabel(s) + ": nobody may write it.");
    if (!s.scope) out.push("⚠ A custom scope needs a name.");
  });
  if (!d.scopes.length) out.push("⚠ Add at least one scope besides the system ones.");
  return out;
}
function addScope(d, key, at){
  const i = at ?? d.scopes.length;
  if (key === "__custom") { if (!d.allow_dynamic_scopes) return toast("Turn on “Allow users to add custom scopes” first");
    let k = 1, name; const have = new Set(d.scopes.map(s => s.scope)); do { name = "custom" + (k > 1 ? k : ""); k++; } while (have.has(name));
    d.scopes.splice(i, 0, {scope:name, job:"", store:"vector", custom:[], events:[]}); }
  else if (d.scopes.some(s => s.scope === key)) return toast("“" + SCOPE_LABEL[key] + "” is already in the template");
  else d.scopes.splice(i, 0, {scope:key, job:"", store:DEF_STORE[key] || "vector", custom:[], events:[]});
  S.tsel = i; render();
}
const sortR = l => l.sort((x, y) => ACCESS.findIndex(a => a.id === x) - ACCESS.findIndex(a => a.id === y));
function addSpaceAccess(d, a){ d.access = d.access || [];
  if (d.access.includes(a)) return refuse(AL[a].label + " is already on the whole space");
  const was = d.access.find(r => KEY(r) === KEY(a) && a !== "technical.exclude"); d.access = sortR([...d.access.filter(r => r !== was), a]);
  S.tsel = "space"; render(); if (was) toast(AL[a].label + " replaces " + AL[was].label + " on the whole space"); }
function addAccess(d, i, a){
  const s = d.scopes[i]; if (!s) return; s.custom = s.custom || [];
  if (s.custom.includes(a)) return refuse(AL[a].label + " is already on " + slabel(s));
  if ((d.access || []).includes(a)) return refuse("The whole space already has " + AL[a].label + ": nothing to customize");
  const was = s.custom.find(r => KEY(r) === KEY(a) && a !== "technical.exclude"); s.custom = sortR([...s.custom.filter(r => r !== was), a]);
  S.tsel = i; render(); if (was) toast(AL[a].label + " replaces " + AL[was].label + " on " + slabel(s));
}
// a refusal stays on screen in the toolbar (not only a toast), so it can be read after the toast is gone
function refuse(msg){ S.tmsg = msg; const m = $("tcMsg"); if (m) m.textContent = msg; toast(msg, 6000); }
function wireSpace(){
  const d = tcur(); if (!d) return;
  const B = $("kcBody");
  // Text fields never redraw on blur: a redraw there would swallow the click on Save that caused the blur.
  const live = () => { $("tcSave").disabled = $("tcReset").disabled = !tdirty(S.tid); };
  $("tcName").oninput = e => { d.name = e.target.value; $("tcCtx").textContent = d.name; live(); };
  $("tcVis").onchange = e => { d.visibility = e.target.value; render(); };
  $("tcDyn").onclick = () => { d.allow_dynamic_scopes = !d.allow_dynamic_scopes; render(); };
  $("tcTech").onclick = () => { d.tech_visible = d.tech_visible === false; render(); };
  $("tcReset").onclick = () => { delete S.tdrafts[S.tid]; if (!S.types[S.tid]) S.tid = S.types.project ? "project" : Object.keys(S.types)[0]; S.tsel = null; S.tmsg = ""; render(); toast("Back to the saved template"); };
  $("tcSave").onclick = saveType;
  $("tcNew").onclick = () => { let id = "template", k = 2; while (S.types[id] || S.tdrafts[id]) id = "template-" + k++;
    S.tdrafts[id] = {version:2, name:"New template", description:"", visibility:"private", allow_dynamic_scopes:true, tech_visible:true, access:["owner.read","owner.write"],
      scopes:[{scope:"about", job:"", store:"sqlite", custom:[], events:[]}], _new:true};
    S.tid = id; S.tsel = null; S.tmsg = ""; render(); $("tcName").select(); };
  const del = $("tcDel"); if (del) del.onclick = () => sure(del, delType);
  const tq = $("tcQ"); tq.oninput = () => { S.tq = tq.value; S.tmax = TPAGE; render(); const n = $("tcQ"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); };
  const more = $("tcMore"); if (more) more.onclick = () => { S.tmax = (S.tmax || TPAGE) + TPAGE; render(); };
  B.querySelectorAll("[data-tid]").forEach(el => el.onclick = () => { S.tid = el.dataset.tid; S.tsel = null; S.tmsg = ""; render(); });
  B.querySelectorAll(".kc-scard.sys").forEach(c => c.onclick = () => toast((SYS_SCOPES.find(y => y.scope === c.dataset.sys) || {}).name + " is a system scope: every space has it, KH keeps it"));
  B.querySelectorAll(".kc-scard[data-i]").forEach(card => {
    const i = +card.dataset.i, s = d.scopes[i];
    card.onclick = e => { if (e.target.closest("input,button,select,[data-rm],.kc-evts")) return; S.tsel = S.tsel === i ? null : i; render(); };
    const jb = card.querySelector("[data-job]"); jb.oninput = () => { s.job = jb.value; live(); };
    const nm = card.querySelector("[data-sname]");
    if (nm) nm.onchange = () => { const v = nm.value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g,"_").replace(/^_+|_+$/g,"").slice(0, 32);
      if (!v) toast("A scope needs a name"); else if (d.scopes.some((x, j) => j !== i && x.scope === v) || SYS_IDS.includes(v)) toast("“" + v + "” is already a scope here"); else s.scope = v; render(); };
    card.querySelector("[data-store]").onchange = e => { s.store = e.target.value; render(); };
    card.querySelectorAll("[data-rm]").forEach(x => x.onclick = () => { s.custom = s.custom.filter(a => a !== x.dataset.rm); render(); });
    card.querySelector("[data-evadd]").onclick = () => openEvent(d, s, null);
    card.querySelectorAll("[data-evx]").forEach(x => x.onclick = ev => { if (ev.target.closest("[data-evrm]")) return; openEvent(d, s, +x.dataset.evx.split(":")[1]); });
    card.querySelectorAll("[data-evrm]").forEach(x => x.onclick = () => { s.events.splice(+x.dataset.evrm.split(":")[1], 1); render(); });
    card.querySelector("[data-sdel]").onclick = () => { d.scopes.splice(i, 1); S.tsel = null; render(); };
    card.querySelector("header").onpointerdown = e => drag(e, {kind:"move", from:i}, "↕ " + slabel(s));
  });
  B.querySelectorAll("[data-scope]").forEach(c => { if (c.classList.contains("used")) return;
    c.onpointerdown = e => drag(e, {kind:"scope", key:c.dataset.scope}, c.dataset.scope === "__custom" ? "+ Custom Scope" : SCOPE_LABEL[c.dataset.scope]);
    c.onclick = () => { if (!S.justDragged) addScope(d, c.dataset.scope); }; });
  const ab = $("tcAccess"); ab.onclick = e => { if (e.target.closest("select,button,[data-srm],.kc-tgl")) return; S.tsel = S.tsel === "space" ? null : "space"; render(); };
  ab.querySelectorAll("[data-srm]").forEach(x => x.onclick = () => { d.access = d.access.filter(a => a !== x.dataset.srm); render(); });
  B.querySelectorAll("[data-acc]").forEach(c => {
    c.onpointerdown = e => drag(e, {kind:"acc", key:c.dataset.acc}, AL[c.dataset.acc].label);
    c.onclick = () => { if (S.justDragged) return; if (S.tsel == null) return toast("Click the Access box or a scope card first, or drag the rule onto it");
      if (S.tsel === "space") addSpaceAccess(d, c.dataset.acc); else addAccess(d, S.tsel, c.dataset.acc); }; });
}
// pointer-event drag (mouse, pen, touch and automated browsers alike)
function drag(e, p, lbl){
  if (e.button !== 0 || e.target.closest("input,select,button,[data-rm]")) return;
  e.preventDefault();   // no text selection while dragging (a click still fires)
  const sx = e.clientX, sy = e.clientY; let ghost = null;
  const clear = () => document.querySelectorAll(".kc-board.over,.kc-scard.over,.kc-access.over").forEach(x => x.classList.remove("over"));
  const move = ev => {
    if (!ghost) { if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 6) return;
      ghost = document.createElement("div"); ghost.className = "kc-ghost"; ghost.textContent = lbl; document.body.appendChild(ghost); document.body.classList.add("kc-dragging"); }
    ev.preventDefault(); ghost.style.left = ev.clientX + "px"; ghost.style.top = ev.clientY + "px"; clear();
    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    if (p.kind === "acc") { const c = el && (el.closest(".kc-scard") || el.closest(".kc-access")); if (c) c.classList.add("over"); }
    else { const b = el && el.closest("#tcBoard"); if (b) b.classList.add("over"); }
  };
  const up = ev => {
    document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", up);
    if (!ghost) return;
    ghost.remove(); document.body.classList.remove("kc-dragging"); clear();
    S.justDragged = true; setTimeout(() => { S.justDragged = false; }, 0);
    const d = tcur(), el = document.elementFromPoint(ev.clientX, ev.clientY);
    if (p.kind === "acc") { if (el && el.closest(".kc-access")) return addSpaceAccess(d, p.key);
      const c = el && el.closest(".kc-scard"); if (!c) return;
      if (c.dataset.sys) return refuse((SYS_SCOPES.find(y => y.scope === c.dataset.sys) || {}).name + " takes no access rules: KH keeps it");
      return addAccess(d, +c.dataset.i, p.key); }
    if (!(el && el.closest("#tcBoard"))) return;
    const cards = [...$("tcBoard").querySelectorAll(".kc-scard[data-i]")]; let at = cards.length;
    for (let k = 0; k < cards.length; k++) { const r = cards[k].getBoundingClientRect(); if (ev.clientY < r.top + r.height / 2) { at = k; break; } }
    if (p.kind === "scope") addScope(d, p.key, at);
    else { const [m] = d.scopes.splice(p.from, 1); const to = at > p.from ? at - 1 : at; d.scopes.splice(to, 0, m); S.tsel = to; render(); }
  };
  document.addEventListener("pointermove", move); document.addEventListener("pointerup", up);
}
function sure(btn, fn){
  if (btn.dataset.sure) return fn();
  const txt = btn.textContent; btn.dataset.sure = "1"; btn.textContent = "Sure? " + txt.toLowerCase(); btn.classList.add("r");
  setTimeout(() => { if (btn.isConnected) { delete btn.dataset.sure; btn.textContent = txt; btn.classList.remove("r"); } }, 4000);
}
// The event action modal: Identity (its name, from what it does) on top, then Settings, then a read-only note.
function openEvent(d, s, j){
  const isNew = j == null, e = isNew ? {when:"count_over", n:100, then:"move_bound"} : clone(s.events[j]);
  const again = () => { const o = evOn(e.when), a = evDo(e.then), low = e.when === "older_than" ? 0 : 1, bad = a.k === "run_flow" && !e.flow ? "Pick the flow it runs." : (o.p && !(e.n >= low)) ? "Give a number of " + low + " or more." : "";
    const el = shell('<span class="ttl">⚡ Event action</span><span class="kc-sub">on scope “' + esc(slabel(s)) + '”</span>',
      area("Identity", row("Event action", '<input type="text" value="' + esc(evText(e)) + '" disabled>', "Named by what it does; on scope “" + esc(slabel(s)) + "”."))
      + area("Settings", row("When", '<div class="kc-pick">' + EV_ON.map(x => '<div data-on="' + x.k + '" class="' + (e.when === x.k ? "on" : "") + '"><b>' + esc(x.l) + '</b><small>' + esc(x.d) + '</small></div>').join("") + '</div>')
        + (o.p ? row(o.pl, '<input type="number" min="' + low + '" data-n value="' + esc(e.n) + '">') : '')
        + row("Then", '<div class="kc-pick">' + EV_DO.map(x => '<div data-do="' + x.k + '" class="' + (e.then === x.k ? "on" : "") + '"><b>' + esc(x.l) + '</b><small>' + esc(x.d) + '</small></div>').join("") + '</div>')
        + (a.p === "flow" ? row("Flow", pickField("evf", e.flow || "", "Pick a flow…")) : '')
        + row("Dry run", '<span class="kc-tgl' + (e.dry_run ? " on" : "") + '" data-dry><i></i>' + (e.dry_run ? "Only report, do not change" : "Off: KH changes the rows") + '</span>', "On: each run only counts what it would do (last run: would delete N)."))
      + area("Note", '<p class="kc-note" id="evNote">' + esc(EV_NOTE) + '</p><p class="kc-note" id="evLast">' + (isNew ? "Not run yet: add it and save the template." : "Last run: looking…") + '</p>'),
      '<span class="iss">' + (bad ? "⚠ " + esc(bad) : "") + '</span><span style="display:flex;gap:10px"><button class="btn g s" data-x>Cancel</button><button class="btn s" data-ok' + (bad ? " disabled" : "") + '>' + (isNew ? "Add" : "Apply") + '</button></span>');
    const keepIn = () => { const n = el.querySelector("[data-n]"); if (n) e.n = +n.value; };
    el.querySelectorAll("[data-on]").forEach(x => x.onclick = () => { keepIn(); if (e.when !== x.dataset.on) { e.when = x.dataset.on; const o2 = evOn(e.when); if (o2.p) e.n = o2.def; } again(); });
    el.querySelectorAll("[data-do]").forEach(x => x.onclick = () => { keepIn(); e.then = x.dataset.do; again(); });
    el.querySelector("[data-dry]").onclick = () => { keepIn(); if (e.dry_run) delete e.dry_run; else e.dry_run = true; again(); };
    if (!isNew) evLastRun(s, s.events[j]);
    const n = el.querySelector("[data-n]"); if (n) n.onchange = () => { keepIn(); again(); };
    const f = el.querySelector("[data-evf]"); if (f) f.onclick = () => { keepIn(); pickFlow({title:"Which flow does it run?", chosen:e.flow, onDone:id => { e.flow = id; again(); }}); };
    el.querySelector("[data-ok]").onclick = () => { keepIn(); if (!evOn(e.when).p) delete e.n; if (e.then !== "run_flow") delete e.flow; if (!e.dry_run) delete e.dry_run;
      s.events = s.events || []; if (isNew) s.events.push(e); else s.events[j] = e; closeShell(); render(); toast("Event action " + (isNew ? "added" : "changed") + ". Save the template to keep it."); }; };
  again();
}
// K32: the last run of a saved action, over the spaces made from this template (audit rows of the scheduler)
async function evLastRun(s, e){
  const sig = [s.scope, e.when, e.n ?? "", e.then, e.flow || ""].join("|");
  try { const r = await api("GET", "/flows/scope-events/runs?type=" + encodeURIComponent(S.tid) + "&sig=" + encodeURIComponent(sig) + "&limit=1"), x = (r.runs || [])[0], el = $("evLast");
    if (el) el.textContent = x ? "Last run: " + x.message + " · " + x.space + " · " + x.ts.replace("T", " ").replace("Z", " UTC") + (x.dry_run ? " (dry run)" : "")
      : !r.spaces.length ? "Not run yet: no space is made from this template." : "Not run yet in " + r.spaces.join(", ") + " (KH checks every " + r.every_s + " s).";
  } catch(err){ const el = $("evLast"); if (el) el.textContent = "Last run: " + err.message; } }
async function saveType(){
  const d = clone(tcur()), isNew = !!d._new, t = S.types[S.tid]; delete d._new; let id = S.tid;
  if (spaceWarnings(d).some(w => w.includes("needs a name"))) return refuse("Name every custom scope first.");
  // kind_map and save_kinds name scopes: sent back only while the scopes are the ones they were made for, else KH derives them
  if (t && JSON.stringify((t.scopes || []).map(s => s.scope).sort()) === JSON.stringify(d.scopes.map(s => s.scope).sort())) {
    if (t.kind_map) d.kind_map = t.kind_map; if (t.save_kinds) d.save_kinds = t.save_kinds; }
  if (isNew) { const base = slug(d.name); id = base; let k = 2; while (S.types[id]) id = base + "-" + k++; }
  try { await api("PUT", "/project-types/" + encodeURIComponent(id), d); delete S.tdrafts[S.tid]; S.tid = id; S.tmsg = "";
    toast("Saved “" + d.name + "”. New spaces start from it; spaces already made keep their snapshot."); loadFlows("space"); }
  catch(e){ refuse(e.message); }
}
async function delType(){
  try { const r = await api("DELETE", "/project-types/" + encodeURIComponent(S.tid)); delete S.tdrafts[S.tid]; S.tmsg = ""; toast("Template " + r.result + ".");
    if (r.result === "deleted") S.tid = "project"; loadFlows("space"); }
  catch(e){ refuse(e.message); }
}

// ═════════════════════════════ MODALS AND PICKERS (shared) ═════════════════════════════
// Every modal is cut into areas, in this order: Identity (name, what it is for, on/off, include in the response) on
// top, then its Settings, then read-only notes. Links between nodes are drawn on the canvas, never set in a modal.
function area(title, inner){ return '<div class="kc-fs"><h4>' + esc(title) + '</h4>' + inner + '</div>'; }
function row(lbl, inner, hint){ return '<div class="kc-fr"><label>' + esc(lbl) + '</label><div>' + inner + (hint ? '<div class="hint">' + hint + '</div>' : '') + '</div></div>'; }
function shell(head, body, foot){
  closeShell(); const el = document.createElement("div"); el.id = "kcForm"; el.className = "kc-mback"; document.body.appendChild(el);
  el.addEventListener("pointerdown", ev => { if (ev.target === el) closeShell(); });
  el.innerHTML = '<div class="kc-modal" style="width:min(720px,100%)" role="dialog" aria-modal="true"><div class="kc-mhd">' + head + '<span class="sp"></span><button class="xb" data-x title="Close (Esc)">✕</button></div><div class="kc-mbody kc-areas">' + body + '</div><div class="kc-mft">' + foot + '</div></div>';
  el.querySelectorAll("[data-x]").forEach(b => b.onclick = closeShell); return el;
}
function closeShell(){ const el = $("kcForm"); if (el) el.remove(); }
// a question on top of a modal, in the page (a browser confirm() would hide what it is about)
function askSure(title, text, yes, fn){ const el = document.createElement("div"); el.id = "kcAsk"; el.className = "kc-mback"; el.style.zIndex = 95; document.body.appendChild(el);
  el.innerHTML = '<div class="kc-modal" style="width:min(460px,100%)" role="alertdialog"><div class="kc-mhd"><span class="ttl">' + esc(title) + '</span></div><div class="kc-mbody"><p class="kc-note" style="margin:0">' + esc(text) + '</p></div>'
    + '<div class="kc-mft"><span class="iss"></span><span style="display:flex;gap:10px"><button class="btn g s" data-no>Cancel</button><button class="btn s" data-yes>' + esc(yes) + '</button></span></div></div>';
  const close = () => el.remove(); el.querySelector("[data-no]").onclick = close; el.querySelector("[data-yes]").onclick = () => { close(); fn(); };
  el.addEventListener("pointerdown", e => { if (e.target === el) close(); }); }
// Small views never list a growing set: they show ONE field; clicking it opens a picker that searches and pages.
const pickField = (attr, text, empty) => '<button type="button" class="kc-pf" data-' + attr + '><span class="t">' + (text ? esc(text) : '<span class="muted">' + esc(empty) + '</span>') + '</span><span class="car">▾</span></button>';
let PK = null;
function recentGet(k){ try { return JSON.parse(localStorage.getItem("kh_home_recent_" + k) || "[]"); } catch(e) { return []; } }
function recentAdd(k, v, n){ try { localStorage.setItem("kh_home_recent_" + k, JSON.stringify([v, ...recentGet(k).filter(x => x !== v)].slice(0, n))); } catch(e) {} }
function pickHost(w){ let el = $("kcPick"); if (!el) { el = document.createElement("div"); el.id = "kcPick"; el.className = "kc-mback"; el.style.zIndex = 85; document.body.appendChild(el);
  el.addEventListener("pointerdown", e => { if (e.target === el) closePick(); }); } el.dataset.w = w; return el; }
function closePick(){ PK = null; const el = $("kcPick"); if (el) el.remove(); }
document.addEventListener("keydown", e => { if (e.key !== "Escape") return; if ($("kcAsk")) { e.stopImmediatePropagation(); $("kcAsk").remove(); } else if (PK) { e.stopImmediatePropagation(); closePick(); } else if ($("kcForm")) { e.stopImmediatePropagation(); closeShell(); } }, true);
const trigWords = f => (f.triggers || []).map(t => t.type === "prompt" ? "Prompt" : ((EVENTS[t.event] || {}).label || t.event)).join(" · ") || "No trigger: runs when another flow runs it";
// The flow picker: one page of GET /flows?q=&trigger=&limit=10&offset= at a time; Recent before anything is typed.
// o = {title, hint, chosen, multi, skip(id), onDone}
function pickFlow(o){ PK = {q:"", trig:"", page:0, ...o, sel:new Set(o.multi ? o.chosen || [] : []), flow:true}; drawPick(); }
async function drawPick(){
  const o = PK; if (!o) return;
  const chosen = o.trig === "chosen";
  let page = {total:0, items:[]};
  try {
    if (chosen) { const ids = [...o.sel]; page = {total:ids.length, items:ids.slice(o.page * 10, o.page * 10 + 10).map(id => ({steps:0, triggers:[], desc:"", ...(S.fcache[id] || {}), id, name:(S.fcache[id] || {}).name || id}))}; }
    else page = await api("GET", "/flows?limit=10&offset=" + o.page * 10 + "&q=" + encodeURIComponent(o.q) + (o.trig ? "&trigger=" + o.trig : ""));
  } catch(e) { page = {total:0, items:[], err:e.message}; }
  if (PK !== o) return;
  page.items.forEach(x => { S.fcache[x.id] = {...(S.fcache[x.id] || {}), ...x}; });
  const skip = o.skip || (() => false), on = id => o.multi ? o.sel.has(id) : o.chosen === id;
  const item = f => { const no = skip(f.id); return '<div data-pk="' + esc(f.id) + '" class="' + (on(f.id) ? "on" : "") + (no ? " off" : "") + '" title="' + (no ? "A flow cannot run itself or a flow that runs it" : "") + '"><b>' + (o.multi ? (on(f.id) ? "☑ " : "☐ ") : "⧉ ") + esc(f.name) + ' <span class="kc-mono kc-hint">' + esc(f.id) + '</span></b><small>' + esc(f.desc || trigWords(f)) + ' · ' + (typeof f.steps === "number" ? f.steps : (f.steps || []).length) + ' steps' + (no ? ' · cannot be picked here' : '') + '</small></div>'; };
  const recent = !o.q && !o.trig && !o.page ? recentGet("flows").filter(id => (S.fcache[id] || {}).name).slice(0, 5).map(id => ({steps:0, triggers:[], desc:"", ...S.fcache[id], id})) : [];
  const chips = [["", "All"], ["event", "Event"], ["prompt", "Prompt"], ["none", "No trigger"]].concat(o.multi ? [["chosen", "Chosen (" + o.sel.size + ")"]] : []);
  const el = pickHost("flow"), pages = Math.max(1, Math.ceil(page.total / 10));
  el.innerHTML = '<div class="kc-modal" style="width:min(640px,100%)"><div class="kc-mhd"><span class="ttl">⧉ ' + esc(o.title) + '</span><span class="sp"></span><button class="xb" data-pkx title="Close (Esc)">✕</button></div><div class="kc-mbody">'
    + (o.hint ? '<p class="kc-hint" style="margin:0">' + esc(o.hint) + '</p>' : '')
    + '<input type="text" class="kc-search" data-pkq placeholder="Search flows by name, id or what they do" value="' + esc(o.q) + '">'
    + '<span class="kc-seg kc-chipsf">' + chips.map(([k, l]) => '<a data-pkt="' + k + '" class="' + (o.trig === k ? "on" : "") + '">' + esc(l) + '</a>').join("") + '</span>'
    + (recent.length ? '<div class="kc-hint">Recent</div><div class="kc-pick">' + recent.map(item).join("") + '</div><div class="kc-hint">All flows</div>' : '')
    + '<div class="kc-pick" id="kcPkList">' + page.items.map(item).join("") + '</div>'
    + (page.err ? '<div class="kc-warn">' + esc(page.err) + '</div>' : page.total ? '' : '<p class="kc-hint">No flow matches.</p>')
    + (page.total > 10 ? '<div class="kc-pager"><button class="btn g s" data-pkp="-1"' + (o.page ? "" : " disabled") + '>‹ Previous</button><span class="kc-hint">Page ' + (o.page + 1) + ' of ' + pages + ' · ' + page.total + ' flows</span><button class="btn g s" data-pkp="1"' + (o.page + 1 < pages ? "" : " disabled") + '>Next ›</button></div>' : '')
    + '</div><div class="kc-mft"><span class="iss"></span><span style="display:flex;gap:10px"><button class="btn g s" data-pkx>Cancel</button>' + (o.multi ? '<button class="btn s" data-pkok>Done</button>' : '') + '</span></div></div>';
  el.querySelectorAll("[data-pkx]").forEach(b => b.onclick = closePick);
  const q = el.querySelector("[data-pkq]"); q.oninput = () => { o.q = q.value; o.page = 0; clearTimeout(o.t); o.t = setTimeout(() => drawPick().then(() => { const n = document.querySelector("[data-pkq]"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } }), 180); };
  if (o.focus !== false) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  el.querySelectorAll("[data-pkt]").forEach(a => a.onclick = () => { o.trig = a.dataset.pkt; o.page = 0; drawPick(); });
  el.querySelectorAll("[data-pkp]").forEach(b => b.onclick = () => { o.page += +b.dataset.pkp; drawPick(); });
  el.querySelectorAll("[data-pk]").forEach(dv => dv.onclick = () => { const id = dv.dataset.pk; if (skip(id)) return toast("A flow cannot run itself or a flow that runs it.");
    if (o.multi) { if (o.sel.has(id)) o.sel.delete(id); else o.sel.add(id); o.focus = false; drawPick(); } else { recentAdd("flows", id, 8); const done = o.onDone; closePick(); done(id); } });
  const ok = el.querySelector("[data-pkok]"); if (ok) ok.onclick = () => { const done = o.onDone, v = [...o.sel]; v.forEach(id => recentAdd("flows", id, 8)); closePick(); done(v); };
}

// ═════════════════════════════ FLOW CONFIG ═════════════════════════════
// KH 7.0 (K24): flows in the v2 model (app/flows/v2.py) on a canvas, ported from proto/kh-flow-config-proto.html.
// Every step has two exits: ✓ (right) when it worked, ✕ (below) when it failed (yes / no for Decide). A target is a
// step id, "@end" (Return Response) or "@fail" (Stop: failed). Links between nodes are drawn on the canvas only:
// pull a dot onto a node, or click a dot to pick the target from a list. A v1 flow (made before KH 7.0) is shown as
// v2, read-only: KH runs it as before.
const END = "@end", FAIL = "@fail", NW = 160, NH = 74, LPAGE = 15;
// what each block is for, one line (the + Add Step menu and the step modal's note)
const DESC = {
  "kh.call":"Read, search or write rows of a scope in a space, with the caller's own rights. Most steps are this one.",
  "data.filter":"Keep only the items of a step that match a condition, e.g. pl.col(\"score\") >= 0.6.",
  "data.merge":"Merge the items of several earlier steps into one list: the same row once, best score first.",
  "data.limit":"Keep only the first N items of the step before, so the answer stays short.",
  "text.format":"Write the items as text for the agent: a heading, a line per item, a text for no items.",
  "text.mask":"Hide tokens, passwords and keys in the text before it goes on.",
  "text.note":"Add a fixed text: an instruction for the agent, or a message on a failed branch.",
  "logic.decide":"Check a condition and go on by its yes or no exit.",
  "agent.task":"Hand the agent running the flow a task, a guide and the shape of its answer; the flow goes on with the answer. KH calls no LLM itself.",
  "end.return":"End a branch as worked: hands back the items so far, with a text of its own on top.",
  "end.stop":"End a branch with its own outcome (failed, deny, quiet) and its own message to the agent.",
  "flow.call":"Run another flow here as one step (or drag a flow from Existing Flows onto the canvas)."};
// kh.call: the settings that belong to each action; the others wait under More options
const KH_OPS = {rows:"Read rows", newest:"Newest rows", search:"Search rows by meaning", write:"Write a row", spaces:"Spaces named in a text", map:"Task map (graph + meaning)",
  expand:"Open one row", find:"Look up a key (address book)", check:"Check a bundle against the save rules", resolve:"Match a bundle to existing rows", upsert:"Save nodes and links",
  walk:"Follow links", group:"Group hits by links", inbox:"My unread messages", match:"Nearest task flow", learn:"Learn a link", fade:"Fade learned links", "move overflow":"Move rows over a limit",
  plans:"Plans of the actions", remember:"Remember the chain (a case)", "record gaps":"Record the gaps", gaps:"Open gaps", housekeep:"Housekeep learned links", levels:"Rebuild the levels"};
// one line each: what the action does (the option text of "What to do")
const KH_OPD = {rows:"the rows of a scope, a Custom filter may narrow them", newest:"the newest rows of a scope", search:"the rows closest in meaning to a text (a Vector scope)",
  write:"adds one row to a scope", spaces:"the spaces a text names", map:"a compact map of the space for a task", expand:"one row's core facts, links and a section",
  find:"the rows a key (name, code, path) points to", check:"what in a bundle breaks the save rules", resolve:"same, maybe or new for each node of a bundle",
  upsert:"writes a bundle's nodes and links", walk:"the rows reached by typed links", group:"hits grouped by their links", inbox:"the caller's unread messages",
  match:"the task flow nearest the prompt", plans:"the ordered rules of the actions named", remember:"the turn's chain as a case (pointers, never copies)",
  "record gaps":"rows the agent opened that the map missed", learn:"a learned link for an open gap", fade:"unconfirmed learned links lose weight", "move overflow":"the oldest hot log rows over the limit go to the logs of their space",
  gaps:"the open gap events of the space", housekeep:"every learned-link guard, in order (maintainers)", levels:"rebuild the grouping levels of the space (maintainers)"};
// the fields each op uses (khcall.py), in the order the modal shows them; an op shows nothing else (K25 + K21c note)
const KH_MAIN = {rows:["space","scope","where"], newest:["space","scope","where"], search:["space","scope","text"], write:["space","scope","text","each"],
  spaces:["text"], map:["space","text"], expand:["space","ids","facet"], find:["space","text"], check:["space","from"], resolve:["space","from"], upsert:["space","from"],
  walk:["space","ids","links","facet"], group:["space","from"], inbox:["space"], match:["text"], learn:["space","from"], fade:["space","days"], "move overflow":[],
  plans:["text"], remember:["space"], "record gaps":["space"], gaps:["space"], housekeep:["space"], levels:["space"]};
// ops that run without a space up front (khcall.TURN_OPS): no 'Pick the space' for them
const TURN_OPS = ["remember", "record gaps", "spaces", "inbox", "match", "plans", "move overflow"];
const CTX_SPACES = [{v:"@bound", l:"The session's bound space", d:"the space the session works on, known when the flow runs"},
  {v:"@profile", l:"The caller's Profile space", d:"the personal space of whoever runs the flow"},
  {v:"@named", l:"The space the prompt names", d:"found in the prompt's text when the flow runs"},
  {v:"@input", l:"The space given with the call", d:"input.space of the payload"}];
let EDGE = {ok:"#a7a3b8", fail:"#ff6ec7", yes:"#4fd1c5", no:"#ffc45e"};
// SVG attributes take no var(): the wire colours are read from the theme's --edge-* tokens at each draw (theme.css)
function edgeColors(){ const cs = getComputedStyle(document.documentElement), g = (k, d) => cs.getPropertyValue(k).trim() || d;
  return {ok:g("--edge-ok", EDGE.ok), fail:g("--edge-fail", EDGE.fail), yes:g("--edge-yes", EDGE.yes), no:g("--edge-no", EDGE.no)}; }
const isEnd = x => !!x && (x.b === "end.return" || x.b === "end.stop");
const isDecide = x => !!x && x.b === "logic.decide";
const blk = b => S.blocks[b] || {icon:"❔", label:b, params:[], group:"?"};
const stepName = x => x.name || (x.b === "flow.call" ? ((S.fcache[x.args.flow] || {}).name || "Flow") : blk(x.b).label);
const fd = () => S.draft;
const fstep = id => (fd().steps || []).find(x => x.id === id);
const firstOf = d => d.first && d.steps.some(x => x.id === d.first) ? d.first : ((d.steps[0] || {}).id || END);
const exitWord = (x, w) => isDecide(x) ? (w === "ok" ? "yes" : "no") : (w === "ok" ? "works" : "fails");
const evLabel = id => (EVENTS[id] || {}).label || id;

// ── the draft: a v2 body; a v1 body is turned into v2 for the canvas (each step runs the next one whatever happens,
// as the v1 engine does), and the flow is read-only
function normV2(f){
  const d = clone({name:f.name || "", desc:f.desc || f.description || "", enabled:f.enabled !== false, triggers:f.triggers || [], first:f.first, ui:f.ui || {},
    grid:f.grid, answer:f.answer || {row:"{text}", part:9000}, steps:f.steps || []});
  if (!d.grid) delete d.grid;
  d.steps.forEach((x, i) => { if (isEnd(x)) { x.next = {}; return; } x.next = x.next || {}; x.next.ok = x.next.ok || ((d.steps[i + 1] || {}).id || END); x.next.fail = x.next.fail || FAIL; x.args = x.args || {}; });
  return d;
}
function fromV1(f){
  const steps = (f.steps || []).map((s, i, all) => { const nx = (all[i + 1] || {}).id || END;
    return {id:s.id, b:s.node, name:s.title || (S.nodes[s.node] || {}).label || s.node, args:s.args || {}, next:{ok:nx, fail:nx}, ...(s.hidden ? {hidden:true} : {}), ...(s.when ? {when:s.when} : {}), ...(s.ui ? {ui:s.ui} : {})}; });
  const seen = {}; steps.forEach(s => { if (seen[s.name]) s.name += " " + (++seen[s.name]); else seen[s.name] = 1; });
  return normV2({name:f.name, desc:f.description, enabled:f.enabled, triggers:f.triggers, steps});
}
const saveBody = d => { const b = clone(d); b.steps.forEach(x => { if (x.b && isEnd(x)) delete x.next; }); return b; };
function fdirty(){ return !!S.draft && !S.v1 && (JSON.stringify(saveBody(S.draft)) !== S.base || !!S.parentsWant); }

async function flowsPage(reset){
  if (reset) { S.list = []; }
  try { const r = await api("GET", "/flows?limit=" + LPAGE + "&offset=" + S.list.length + "&q=" + encodeURIComponent(S.lq || ""));
    S.ltotal = r.total; r.items.forEach(x => { S.fcache[x.id] = {...(S.fcache[x.id] || {}), ...x}; }); S.list = S.list.concat(r.items); }
  catch(e) { S.lerr = e.message; }
}
async function openFlow(id, from){
  try { const f = await api("GET", "/flows/" + encodeURIComponent(id) + "?check=1");
    if (S.test && S.test.fid !== id) { S.test = null; S.locks = {}; }
    S.fid = id; S.from = from || null; S.sel = null; S.v1 = !isV2(f); S.resetScroll = true; S.parentsWant = null; delete S.isNew; S.meta = f; S.kind = f.kind; S.fmsg = ""; S.warn = [];
    S.draft = S.v1 ? fromV1(f) : normV2(f); S.base = JSON.stringify(saveBody(S.draft));
    S.fcache[id] = {...(S.fcache[id] || {}), id, name:f.name, triggers:f.triggers, kind:f.kind};
    recentAdd("flows", id, 8);
    S.sprob = f.problems || [];
    await childNames(); await parentsOf(id); await prepChecks();
  } catch(e) { toast(e.message, 8000); }
  render();
}
const isV2 = f => (f.steps || []).some(x => x && x.b) || (!(f.steps || []).length && ("triggers" in f || "first" in f) && !f.trigger);
// the names of the flows that flow.call steps run (one GET each, cached)
async function childNames(){
  const need = [...new Set((S.draft.steps || []).filter(x => x.b === "flow.call" && x.args.flow && !(S.fcache[x.args.flow] || {}).name).map(x => x.args.flow))];
  await Promise.all(need.map(id => api("GET", "/flows/" + encodeURIComponent(id)).then(f => { S.fcache[id] = {...(S.fcache[id] || {}), id, name:f.name, triggers:f.triggers}; }).catch(() => {})));
}
// the flows that run this one as a step: run_by of its list row (KH computes it)
async function parentsOf(id){
  try { const r = await api("GET", "/flows?limit=100&q=" + encodeURIComponent(id)); const row = r.items.find(x => x.id === id); S.parents = row ? row.run_by || [] : []; }
  catch(e) { S.parents = []; }
}

function flowHtml(){
  const d = fd();
  const tags = f => (f.triggers || []).map(t => t.type === "prompt" ? '<span class="kc-tag task">Prompt</span>' : '<span class="kc-tag' + ((EVENTS[t.event] || {}).hook ? "" : " ev") + '">' + esc(evLabel(t.event)) + '</span>').join(" ")
    || '<span class="kc-tag none">No trigger</span>';
  const list = '<input type="text" class="kc-search" id="ffQ" placeholder="Search flows by name, id or what they do" value="' + esc(S.lq || "") + '">'
    + '<div class="kc-src" id="ffList">' + S.list.map(f => '<div class="kc-item' + (f.id === S.fid ? " on" : "") + '" data-fid="' + esc(f.id) + '" title="Click to open; drag onto the canvas to run it as a step">'
      + '<b>' + esc(f.name) + '</b><span class="kc-tags">' + tags(f) + ' <span class="kc-tag n">' + f.steps + ' step' + (f.steps === 1 ? "" : "s") + '</span>'
      + ((f.run_by || []).length ? ' <span class="kc-tag n">run by ' + f.run_by.length + ' flow' + (f.run_by.length > 1 ? "s" : "") + '</span>' : '') + (f.enabled === false ? ' <span class="kc-tag dirty">off</span>' : '') + '</span>'
      + '<small>' + esc((f.desc || "").slice(0, 110)) + '</small><small class="kc-mono">' + esc(f.id) + '</small></div>').join("")
    + (S.list.length < S.ltotal ? '<button class="btn g s" id="ffMore" style="align-self:flex-start">Show ' + Math.min(LPAGE, S.ltotal - S.list.length) + ' more (' + S.list.length + ' of ' + S.ltotal + ')</button>' : '')
    + (S.ltotal ? '' : '<p class="kc-hint">No flow matches.</p>') + '</div>';
  const right = '<div class="kc-col"><h3>Existing Flows <small>' + (S.ltotal || 0) + '</small></h3><div class="card kc-panel">' + list + '</div></div>';
  if (!d) return toolbarFlow() + waitHtml() + '<section class="kc-fc"><div class="card kc-panel"><p class="muted">Pick a flow, or make a new one.</p></div>' + right + '</section>';
  const mine = d.steps.map(x => problem(x)), srv = (fdirty() ? [] : S.sprob || []).filter(w => !mine.some(m => m && (m.startsWith(w) || w.includes(m))));
  const iss = d.steps.map((x, i) => mine[i] ? "“" + stepName(x) + "”: " + mine[i] : "").filter(Boolean).concat(S.warn || [], srv);
  return toolbarFlow() + waitHtml()
    + '<section class="kc-fc"><div class="kc-col">'
    + (S.v1 ? '<div class="kc-warn kc-v1">This flow is kept in the v1 shape: KH runs it as before. The canvas shows it as v2 (each step goes on to the next one whatever happens) and is read-only here.</div>' : '')
    + ((S.parents || []).length ? '<p class="kc-hint" style="margin:0 0 8px">⧉ Run as a step by: ' + S.parents.map(p => esc((S.fcache[p] || {}).name || p)).join(" · ") + '. A change here shows in each of them.</p>' : '')
    + '<div class="card kc-panel"><div class="kc-canvas" id="ffCanvas"><div class="kc-cv" id="ffCv"></div></div>'
    + '<p class="kc-hint">Drag a flow from Existing Flows onto the canvas to run it as a step. Drag a step to move it. Pull its <b>✓</b> dot (right) onto the step that runs when it works, its <b>✕</b> dot (below) onto the step that runs when it fails; click a dot to pick from a list. Click a node to set it up.</p>'
    + (iss.length ? '<div class="kc-warn" id="ffIss">' + iss.map(x => (x[0] === "⚠" ? "" : "⚠ ") + esc(x)).join("<br>") + '</div>' : '')
    + '<div class="kc-foot"><button class="btn g s" id="ffAdd"' + (S.v1 ? " disabled" : "") + '>+ Add Step</button><span class="kc-hint">' + d.steps.length + ' steps</span></div></div>'
    + tryHtml()
    + '</div>' + right + '</section>';
}
function toolbarFlow(){
  const d = fd(), m = S.meta || {};
  return '<div class="kc-tbar"><span class="ctx">Flow <b>' + esc(d ? d.name : "none") + '</b> ' + (S.fid ? '<span class="kc-mono">' + esc(S.fid) + '</span> · v' + (m.version || 1) + (m.seeded ? " · seeded" : "") : "") + '</span>'
    + (S.from ? '<button class="btn g s" id="ffBack">← Back to ' + esc((S.fcache[S.from] || {}).name || S.from) + '</button>' : '')
    + '<button class="btn g s" id="ffNew">+ New flow</button>' + (d && S.meta ? '<button class="btn g s" id="ffDel">' + (m.seeded ? "Reset to seed" : "Delete") + '</button>' : '')
    + '<span class="sp"></span><span class="kc-msg" id="ffMsg">' + esc(S.fmsg || "") + '</span>'
    + '<button class="btn g s" id="ffCancel"' + (fdirty() ? "" : " disabled") + '>Cancel</button><button class="btn s" id="ffSave"' + (fdirty() ? "" : " disabled") + '>Save</button></div>';
}
// what is wrong with a step, before KH is asked (KH checks it all again on Save; K25: the same words as KH's own problems)
function problem(x){
  if (x.b === "flow.call" && !x.args.flow) return "Pick the flow it runs.";
  if (x.b === "logic.decide") { const a = x.args;
    if (condMode(a) === "chips") { if (!(a.rules || []).length) return "Add a condition: + Add condition picks an output.";
      const ri = a.rules.map(ruleIssue).find(Boolean); if (ri) return "In “When”, " + ri + "."; }
    else if (!String(a.cond || "").trim()) return "Give the condition (a Polars expression).";
    const e = exprErr(x, "cond"); if (e) return e; }
  if (x.b === "kh.call") { const op = x.args.op || "rows", need = KH_MAIN[op] || [], sp = x.args.space, sc = x.args.scope;
    if (need.includes("space") && !sp && !TURN_OPS.includes(op)) return "Pick the space.";
    if (sp && S.gone[sp]) return 'Space "' + sp + '" does not exist (deleted?): pick another.';
    if (need.includes("scope") && !sc) return "Pick the scope.";
    if (need.includes("scope") && sp && sp[0] !== "@" && sc && sc !== BIND_SC && (S.scopes[sp] || []).length && !S.scopes[sp].includes(sc)) return "“" + sc + "” is not a scope of " + sp + ".";
    if (op === "search" && sc) { const st = storeIn(sp, sc); if (st && st !== "vector") return 'Search by meaning needs a Vector scope; "' + sc + '" is stored in ' + st; }
    if (need.includes("where")) { const e = exprErr(x, "where"); if (e) return e; } }
  if (x.b === "agent.task") { if (!String(x.args.task || "").trim()) return "Give the task the agent gets.";
    const si = shapeIssue(String(x.args.shape || "").trim()); if (si) return "“Answer must look like”: " + si; }
  const sw = (S.warn || []).find(w => w.includes('step "' + x.name + '"')); if (sw) return sw.replace(/^step "[^"]+": /, "");
  return "";
}
// before the canvas is drawn: the scopes and stores of the named spaces, and KH's check of every Polars field
async function prepChecks(){ const d = S.draft; if (!d) return;
  await Promise.all([...new Set(d.steps.filter(x => x.b === "kh.call" && x.args.space && x.args.space[0] !== "@").map(x => x.args.space))].map(loadScopes));
  await Promise.all(d.steps.flatMap(x => ["where", "cond"].filter(k => x.args && String(x.args[k] || "").trim()).map(k => checkExpr(x, k)))); }

// ── layout: a ✓ target one column right, a ✕ target right below; the two ends in the last column. A node someone
// dragged keeps its place (step.ui, flow.ui.start / @end / @fail); a flow with a grid starts from the grid.
let LAY = {};
function layout(){
  const d = fd(), cell = {}, used = new Set(), put = (id, c, r) => { while (used.has(c + ":" + r)) r++; used.add(c + ":" + r); cell[id] = {c, r}; };
  used.add("0:0"); const q = []; const f = firstOf(d); if (f[0] !== "@") { put(f, 1, 0); q.push(f); }
  while (q.length) { const id = q.shift(), x = fstep(id), at = cell[id]; if (!x) continue;
    [["ok", at.c + 1, at.r], ["fail", at.c, at.r + 1]].forEach(([w, c, r]) => { const t = (x.next || {})[w]; if (t && t[0] !== "@" && !cell[t] && fstep(t)) { put(t, c, r); q.push(t); } }); }
  let maxR = Math.max(0, ...Object.values(cell).map(v => v.r));
  d.steps.forEach(x => { if (!cell[x.id]) put(x.id, 1 + Object.keys(cell).length % 3, maxR + 2); });
  const maxC = Math.max(1, ...Object.values(cell).map(v => v.c)); maxR = Math.max(0, ...Object.values(cell).map(v => v.r));
  cell[END] = {c:maxC + 1, r:0}; cell[FAIL] = {c:maxC + 1, r:Math.max(1, maxR)}; cell.start = {c:0, r:0};
  return Object.fromEntries(Object.entries(cell).map(([k, v]) => [k, {x:20 + v.c * 205, y:36 + v.r * 130}]));
}
function placeGrid(d){
  const host = $("ffCanvas"), cols = Math.max(4, Math.min(8, Math.floor(((host && host.clientWidth) || 900) / 205)));
  const by = n => n === "@start" ? "start" : n === "@end" ? END : n === "@fail" ? FAIL : (d.steps.find(x => x.name === n || x.id === n) || {}).id;
  // a grid row longer than the canvas wraps; a cell already taken (a pin clamped to the last column, a wrapped row)
  // moves on to the next free cell, so no two nodes ever sit on each other
  const out = {}, used = new Set(); let r = 0;
  (d.grid || []).forEach(rw => { let c = 0, top = r; (Array.isArray(rw) ? rw : [rw]).forEach(cell => { const [n, pin] = Array.isArray(cell) ? cell : [cell, null]; const id = by(n); if (!id) return;
    if (pin !== null && pin !== undefined) { c = Math.min(pin, cols - 1); r = top; } else if (c >= cols) { r++; c = 0; }
    while (used.has(c + ":" + r)) { c++; if (c >= cols) { r++; c = 0; } }
    used.add(c + ":" + r); out[id] = {x:20 + c * 205, y:36 + r * 140}; c++; });
    r = Math.max(r, ...[...used].map(k => +k.split(":")[1])) + 1; });
  return out;
}
function posOf(key){ const d = fd(), u = d.ui || {};
  if (key === "start" || key === END || key === FAIL) return u[key] || LAY[key] || {x:20, y:36};
  const x = fstep(key); return (x && x.ui) || LAY[key] || {x:20, y:36}; }
function setPos(key, p){ const d = fd(); if (key === "start" || key === END || key === FAIL) { d.ui = d.ui || {}; d.ui[key] = p; } else fstep(key).ui = p; }
// The Start node: one exit per way the flow starts (each trigger, and 'as a step' when other flows run it). KH starts
// every run at the same first step, so all exits lead there; pulling any of them changes that first step.
function startExits(d){ const t = d.triggers || [], ex = t.map((x, k) => ({w:"t" + k, l:x.type === "prompt" ? "prompt" : evLabel(x.event)}));
  if (!t.length || (S.parentsWant || S.parents || []).length) ex.push({w:"ok", l:t.length ? "as a step" : "first"});
  return ex; }
const startH = d => Math.max(NH, 26 + startExits(d).length * 20);
const portY = (d, w) => { const ex = startExits(d); return ex.length === 1 ? NH / 2 : 30 + Math.max(0, ex.findIndex(e => e.w === w)) * 20; };
function reached(){ const d = fd(), seen = new Set(), todo = [firstOf(d)];
  while (todo.length) { const id = todo.pop(); if (!id || id[0] === "@" || seen.has(id)) continue; seen.add(id); const x = fstep(id); if (x && x.next) todo.push(x.next.ok, x.next.fail); }
  return seen; }
function curve(p, dir, t){
  let a, c2; if (t.x > p.x + 30) { a = {x:t.x, y:t.y + NH / 2}; c2 = {x:a.x - 60, y:a.y}; } else if (t.y > p.y) { a = {x:t.x + NW / 2, y:t.y}; c2 = {x:a.x, y:a.y - 60}; } else { a = {x:t.x + NW / 2, y:t.y + NH}; c2 = {x:a.x, y:a.y + 60}; }
  const c1 = dir === "r" ? {x:p.x + 60, y:p.y} : {x:p.x, y:p.y + 60}, m = (u, v, w, z) => .125 * u + .375 * v + .375 * w + .125 * z;
  return {d:`M${p.x},${p.y} C${c1.x},${c1.y} ${c2.x},${c2.y} ${a.x},${a.y}`, lx:m(p.x, c1.x, c2.x, a.x), ly:m(p.y, c1.y, c2.y, a.y)};
}
function startTitle(d){ const t = d.triggers || []; if (!t.length) return "Run by another flow"; if (t.length > 1) return t.length + " triggers";
  return t[0].type === "prompt" ? "Prompt is like…" : "On " + evLabel(t[0].event); }
function startSub(d){ const t = d.triggers || [], par = S.parentsWant || S.parents || []; if (!t.length) return par.length ? "as a step of " + par.length + " flow(s)" : "no trigger yet";
  return t.map(x => x.type === "prompt" ? ((x.examples || [])[0] ? "“" + x.examples[0] + "”" : "prompt") : evLabel(x.event)).join(" · "); }
// The ⛔ Stop: failed end is drawn only when a ✕ exit leads to it (Rowan 2026-11-09: an end nothing leads to looked like a
// dead node in 8 of the 14 flows). It shows again while a wire is pulled, so it can still be wired by dragging.
const failUsed = d => d.steps.some(x => !isEnd(x) && x.next && x.next.fail === FAIL);
window.drawCanvas = (...a) => drawCanvas(...a);
function drawCanvas(){
  const d = fd(), cv = $("ffCv"); if (!d || !cv) return;
  EDGE = edgeColors();
  const seen = reached(), R = runView(); LAY = layout();
  if (d.grid && !d.gridMoved) { const g = placeGrid(d); Object.assign(LAY, g); }
  const showFail = failUsed(d) || S.sel === FAIL || !!S.showFail;
  const keys = ["start", ...d.steps.map(x => x.id), END, ...(showFail ? [FAIL] : [])], P = Object.fromEntries(keys.map(k => [k, posOf(k)]));
  const W = Math.max(...keys.map(k => P[k].x)) + NW + 80, H = Math.max(...keys.map(k => P[k].y)) + NH + 80;
  cv.style.width = W + "px"; cv.style.height = H + "px";
  let svg = '<svg width="' + W + '" height="' + H + '"><defs>' + Object.entries(EDGE).map(([k, c]) => '<marker id="kcAr-' + k + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M1,1 L9,5 L1,9" fill="none" stroke="' + c + '" stroke-width="1.6"/></marker>').join("") + '</defs>', labs = "";
  const edge = (from, dir, to, kind, text, oy) => { if (!P[to]) return; const p = P[from], o = dir === "r" ? {x:p.x + NW + 8, y:p.y + (oy ?? NH / 2)} : {x:p.x + NW / 2, y:p.y + NH + 8}, r = curve(o, dir, P[to]);
    svg += '<path class="kc-edge' + (R && !R.took(from, kind) ? " off" : "") + '" data-edge="' + from + '|' + kind + '" d="' + r.d + '" fill="none" stroke="' + EDGE[kind] + '" stroke-width="1.7"' + (kind === "fail" || kind === "no" ? ' stroke-dasharray="6 4"' : '') + ' marker-end="url(#kcAr-' + kind + ')"/>';
    if (text) labs += '<span class="kc-elab ' + kind + (R && !R.took(from, kind) ? " off" : "") + '" style="left:' + r.lx + 'px;top:' + r.ly + 'px">' + text + '</span>'; };
  const ex = startExits(d); ex.forEach(e => edge("start", "r", firstOf(d), "ok", ex.length > 1 ? "" : "first", portY(d, e.w)));
  d.steps.forEach(x => { if (isEnd(x)) return; if (isDecide(x)) { edge(x.id, "r", x.next.ok, "yes", "yes"); edge(x.id, "d", x.next.fail, "no", "no"); }
    else { edge(x.id, "r", x.next.ok, "ok", "works"); edge(x.id, "d", x.next.fail, "fail", "fails"); } });
  svg += '<path id="ffTmp" fill="none" stroke="' + (getComputedStyle(document.documentElement).getPropertyValue("--edge-tmp").trim() || "#a96bff") + '" stroke-width="1.8" stroke-dasharray="4 3"/></svg>';
  const at = k => 'left:' + P[k].x + 'px;top:' + P[k].y + 'px';
  let nodes = '<div class="kc-node trig' + (ex.length > 1 ? " multi" : "") + (S.sel === "start" ? " sel" : "") + '" data-key="start" style="' + at("start") + ';height:' + startH(d) + 'px" title="Click to set what starts this flow"><span class="badge st">start</span><b>⚡ ' + esc(startTitle(d)) + '</b><small>' + esc(startSub(d)) + '</small>'
    + ex.map(e => '<span class="port ok" data-port="start|' + e.w + '" style="top:' + portY(d, e.w) + 'px;margin-top:-8px" title="' + esc(e.l) + ': drag onto the step that runs first"></span>' + (ex.length > 1 ? '<span class="plab" style="top:' + (portY(d, e.w) - 5) + 'px">' + esc(e.l) + '</span>' : '')).join("") + '</div>'
    + '<div class="kc-node end' + (S.sel === END ? " sel" : "") + (R ? R.cls(END) : "") + '" data-key="' + END + '" style="' + at(END) + '">' + (R ? R.badge(END) : "") + '<span class="badge en">end · worked</span><b>📦 Return Response</b><small>' + ((d.triggers || []).some(t => (EVENTS[t.event] || {}).hook) ? "list of items · in parts" : "list of items to the agent") + '</small></div>'
    + (showFail ? '<div class="kc-node fail' + (S.sel === FAIL ? " sel" : "") + (R ? R.cls(FAIL) : "") + '" data-key="' + FAIL + '" style="' + at(FAIL) + '">' + (R ? R.badge(FAIL) : "") + '<span class="badge fl">end · failed</span><b>⛔ Stop: failed</b><small>the failing step’s reason</small></div>' : '');
  nodes += d.steps.map(x => {
    const b = blk(x.b), bad = problem(x), lost = !seen.has(x.id), grp = x.b === "flow.call";
    if (isEnd(x)) { const stop = x.b === "end.stop";
      return '<div class="kc-node ' + (stop ? "fail" : "end") + (S.sel === x.id ? " sel" : "") + (lost ? " lost" : "") + (R ? R.cls(x.id) : "") + '" data-key="' + x.id + '" style="' + at(x.id) + '" title="' + (lost ? "Not reached: no arrow leads here" : "Click to set how this branch ends") + '">' + (R ? R.badge(x.id) : "")
        + '<span class="badge ' + (stop ? "fl" : "en") + '">' + (lost ? "Not reached" : stop ? "end · " + (x.args.outcome || "failed") : "end · worked") + '</span>'
        + '<b>' + b.icon + ' ' + esc(stepName(x)) + '</b><small>' + esc(stop ? (x.args.message || "no message") : x.args.lead ? "“" + x.args.lead + "” + items" : "items so far") + '</small>' + (S.v1 ? '' : '<button class="del" data-ndel="' + x.id + '" title="Remove">✕</button>') + '</div>'; }
    const sub = grp ? "⧉ runs a flow" : x.b === "kh.call" ? (KH_OPS[x.args.op] || x.args.op || "") + (x.args.scope ? " · " + x.args.scope : "") : b.label;
    const lock = (S.locks || {})[x.id];
    return '<div class="kc-node' + (grp ? " grp" : "") + (S.sel === x.id ? " sel" : "") + (bad ? " bad" : "") + (lost ? " lost" : "") + (x.hidden ? " hid" : "") + (R ? R.cls(x.id) : "") + '" data-key="' + x.id + '" style="' + at(x.id) + '" title="' + esc(lock ? "🔒 " + lock : lost ? "Not reached: no arrow leads here" : bad || DESC[x.b] || "") + '">' + (R ? R.badge(x.id) : "") + (lock ? '<span class="kc-lock" title="' + esc(lock) + '">🔒</span>' : "")
      + (bad ? '<span class="badge warn">⚠</span>' : lost ? '<span class="badge warn">Not reached</span>' : '')
      + '<b>' + (grp ? "⧉ " : b.icon + " ") + esc(stepName(x)) + '</b><small>' + esc(sub) + '</small>' + (x.hidden ? '<span class="kc-nfr" title="Its items are not handed back; later steps still use them">not in final response</span>' : '')
      + '<span class="port ' + (isDecide(x) ? "yes" : "ok") + '" data-port="' + x.id + '|ok" title="When it ' + exitWord(x, "ok") + ': drag onto the next step"></span>'
      + '<span class="port ' + (isDecide(x) ? "no" : "fail") + '" data-port="' + x.id + '|fail" title="When it ' + exitWord(x, "fail") + ': drag onto a step"></span>'
      + (S.v1 ? '' : '<button class="del" data-ndel="' + x.id + '" title="Remove step">✕</button>') + '</div>'; }).join("");
  cv.innerHTML = svg + labs + nodes + (d.steps.length ? "" : '<div class="kc-elab" style="left:' + (225 + NW / 2) + 'px;top:' + (36 + NH / 2) + 'px;color:var(--vio)">No steps yet: + Add Step</div>');
}
function wireFlow(){
  const B = $("kcBody");
  B.querySelectorAll("[data-fid]").forEach(el => {
    el.onpointerdown = e => listDrag(e, el.dataset.fid);
    el.onclick = () => { if (S.justDragged) return; if (fdirty() && !confirm("Leave “" + fd().name + "” without saving?")) return; openFlow(el.dataset.fid); }; });
  const q = $("ffQ"); q.oninput = () => { S.lq = q.value; clearTimeout(S.lqT); S.lqT = setTimeout(async () => { await flowsPage(true); render(); const n = $("ffQ"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }, 200); };
  const more = $("ffMore"); if (more) more.onclick = async () => { await flowsPage(false); render(); };
  $("ffNew").onclick = () => openNew();
  const bk = $("ffBack"); if (bk) bk.onclick = () => openFlow(S.from);
  const del = $("ffDel"); if (del) del.onclick = () => sure(del, delFlow);
  $("ffCancel").onclick = () => { S.draft = JSON.parse(S.base); S.draft = normV2(S.draft); S.sel = null; S.fmsg = ""; render(); toast("Back to the saved flow"); };
  $("ffSave").onclick = () => saveFlow();
  wireWait();
  if (!fd()) return;
  drawCanvas();
  $("ffAdd").onclick = e => { const r = e.target.getBoundingClientRect(); addMenu(r.left, r.bottom + 6); };
  wireTry();
  const C = $("ffCanvas");
  const pt = e => { const r = $("ffCv").getBoundingClientRect(); return {x:e.clientX - r.left, y:e.clientY - r.top}; };
  C.onpointerdown = e => {
    if (e.button !== 0 || e.target.closest(".del")) return;
    const port = e.target.closest("[data-port]"), node = e.target.closest(".kc-node[data-key]");
    if (port) { e.preventDefault(); if (S.v1) return toast("A v1 flow is read-only here."); const [from, w] = port.dataset.port.split("|"); return pull(e, from, w, pt); }
    if (!node) { if (S.sel !== null) { S.sel = null; drawCanvas(); } return; }
    e.preventDefault();
    const key = node.dataset.key, p0 = posOf(key), sx = e.clientX, sy = e.clientY; let moved = false;
    // the first move pins every node where it is now, so the others never jump to another layout
    const mv = ev => { if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 5) return; if (!moved) { pinAll(); if (fd().grid) fd().gridMoved = true; } moved = true;
      setPos(key, {x:Math.max(0, Math.round((p0.x + ev.clientX - sx) / 10) * 10), y:Math.max(16, Math.round((p0.y + ev.clientY - sy) / 10) * 10)}); drawCanvas(); };
    const up = () => { document.removeEventListener("pointermove", mv); document.removeEventListener("pointerup", up);
      if (moved) { render(); return; }
      S.sel = key; drawCanvas();
      if (key === "start") openStart(); else if (key === END) openAnswer(); else if (key === FAIL) openFail(); else if (isEnd(fstep(key))) openEnd(key); else openStep(key); };
    document.addEventListener("pointermove", mv); document.addEventListener("pointerup", up);
  };
  C.onclick = e => { const x = e.target.closest("[data-ndel]"); if (x) removeStep(x.dataset.ndel); };
}
// Once one node is dragged, every node keeps the place it has now, so the others do not jump to a new auto layout.
function pinAll(){ const d = fd(); ["start", END, FAIL].forEach(k => { d.ui = d.ui || {}; if (!d.ui[k]) d.ui[k] = posOf(k); }); d.steps.forEach(x => { if (!x.ui) x.ui = posOf(x.id); }); }
// Pull an exit onto a target; a click without pulling opens a list of targets instead.
function pull(e, from, w, pt){
  const d = fd(), fromX = from === "start" ? null : fstep(from), p = posOf(from);
  const o = from === "start" ? {x:p.x + NW + 8, y:p.y + portY(d, w)} : w === "ok" ? {x:p.x + NW + 8, y:p.y + NH / 2} : {x:p.x + NW / 2, y:p.y + NH + 8};
  const sx = e.clientX, sy = e.clientY; let moved = false;
  if (from !== "start" && !failUsed(d)) { S.showFail = true; drawCanvas(); }   // the hidden ⛔ end is a drop target while pulling
  const over = ev => { const t = document.elementFromPoint(ev.clientX, ev.clientY), n = t && t.closest(".kc-node[data-key]"); return n && n.dataset.key !== from && n.dataset.key !== "start" ? n : null; };
  const mv = ev => { if (Math.hypot(ev.clientX - sx, ev.clientY - sy) > 4) moved = true; const q = pt(ev); $("ffTmp").setAttribute("d", `M${o.x},${o.y} L${q.x},${q.y}`);
    document.querySelectorAll(".kc-node.drop").forEach(x => x.classList.remove("drop")); const n = over(ev); if (n) n.classList.add("drop"); };
  const up = ev => { document.removeEventListener("pointermove", mv); document.removeEventListener("pointerup", up);
    const n0 = over(ev); S.showFail = false;
    if (!moved) { const opts = d.steps.filter(x => x.id !== from).map(x => ({label:(x.b === "flow.call" ? "⧉ " : blk(x.b).icon + " ") + stepName(x), sub:blk(x.b).label, v:x.id}))
        .concat(from === "start" ? [] : [{label:"📦 Return Response", sub:"end · worked", v:END}, {label:"⛔ Stop: failed", sub:"end · failed", v:FAIL}]);
      menu(ev.clientX, ev.clientY, [{head:from === "start" ? "Runs first" : "When “" + stepName(fromX) + "” " + exitWord(fromX, w) + ", go to"}, ...opts], it => { setEdge(from, w, it.v); render(); }); drawCanvas(); return; }
    const n = n0; if (n) { setEdge(from, w, n.dataset.key); render(); toast(from === "start" ? "“" + tgtLabel(n.dataset.key) + "” now runs first." : "When “" + stepName(fromX) + "” " + exitWord(fromX, w) + ": " + tgtLabel(n.dataset.key) + "."); }
    else drawCanvas(); };
  document.addEventListener("pointermove", mv); document.addEventListener("pointerup", up);
}
const tgtLabel = id => id === END ? "📦 Return Response" : id === FAIL ? "⛔ Stop: failed" : stepName(fstep(id) || {b:"?"});
function setEdge(from, w, to){ const d = fd(); if (from === "start") { d.first = to; return; } const x = fstep(from); x.next = x.next || {}; x.next[w] = to; }
// Remove a step: every link that led to it (✓ or ✕) now goes where it went when it worked (its ✓ target; an end
// leaves Return Response), so a branch that went on through it still goes on.
function removeStep(id){ const d = fd(), x = fstep(id); if (!x) return; const to = (x.next || {}).ok || END;
  d.steps.forEach(y => { if (!y.next) return; if (y.next.ok === id) y.next.ok = to; if (y.next.fail === id) y.next.fail = to; });
  if (d.first === id) d.first = to[0] === "@" ? undefined : to; if (!d.first) delete d.first;
  d.steps.splice(d.steps.indexOf(x), 1); S.sel = null; render(); toast("Removed. Arrows that led to it now go where it went."); }
const nid = d => { const used = new Set(d.steps.map(x => x.id)); let k = d.steps.length + 1; while (used.has("s" + k)) k++; return "s" + k; };
function uniqName(d, base){ const have = new Set(d.steps.map(x => x.name)); let n = base, k = 2; while (have.has(n)) n = base + " " + k++; return n; }
function newStep(b, args){ const d = fd(), bl = blk(b), a = {};
  (bl.params || []).forEach(p => { if (p.def !== undefined && p.def !== null && p.def !== "" && !(Array.isArray(p.def) && !p.def.length)) a[p.k] = clone(p.def); });
  if (b === "kh.call" && !a.op) a.op = "rows";
  return {id:nid(d), b, name:uniqName(d, b === "flow.call" && args && args.flow ? ((S.fcache[args.flow] || {}).name || "Flow") : bl.label), args:{...a, ...(args || {})}, next:{ok:END, fail:FAIL}}; }
// put a new step after the selected one (or at the end) and wire it in
function placeNew(n){ const d = fd(), sel = S.sel && fstep(S.sel);
  if (sel && !isEnd(sel)) { n.next = {ok:sel.next.ok, fail:FAIL}; sel.next.ok = n.id; d.steps.splice(d.steps.indexOf(sel) + 1, 0, n); const p = posOf(sel.id); n.ui = {x:p.x + 205, y:p.y}; }
  else { const last = [...d.steps].reverse().find(y => !isEnd(y) && y.next && y.next.ok === END); n.next = {ok:END, fail:FAIL};
    if (last) last.next.ok = n.id; else if (!d.steps.filter(y => !isEnd(y)).length) d.first = n.id; d.steps.push(n);
    const lp = last ? posOf(last.id) : null; if (lp) n.ui = {x:lp.x + 205, y:lp.y}; }
  S.sel = n.id; }
// an end added with a step selected becomes that step's exit: a Stop takes its ✕, a Return its ✓
function placeEnd(n){ const d = fd(), sel = S.sel && fstep(S.sel); n.next = {};
  if (sel && !isEnd(sel)) { sel.next[n.b === "end.stop" ? "fail" : "ok"] = n.id; const p = posOf(sel.id); n.ui = n.b === "end.stop" ? {x:p.x, y:p.y + 130} : {x:p.x + 205, y:p.y}; }
  d.steps.push(n); S.sel = n.id; }
let menuEl = null;
function closeMenu(){ if (menuEl) { menuEl.remove(); menuEl = null; } }
function menu(x, y, items, pick, wide){
  closeMenu(); menuEl = document.createElement("div"); menuEl.className = "kc-menu" + (wide ? " wide" : "");
  menuEl.innerHTML = items.map((it, i) => it.head ? '<div class="mh">' + esc(it.head) + '</div>' : '<div class="two" data-mi="' + i + '"><b>' + esc(it.label) + '</b>' + (it.sub ? '<small>' + esc(it.sub) + '</small>' : '') + '</div>').join("");
  document.body.appendChild(menuEl);
  const r = menuEl.getBoundingClientRect();
  menuEl.style.left = Math.max(8, Math.min(x, innerWidth - r.width - 10)) + "px"; menuEl.style.top = Math.max(8, Math.min(y, innerHeight - r.height - 10)) + "px";
  menuEl.onclick = e => { const el = e.target.closest("[data-mi]"); if (!el) return; const it = items[+el.dataset.mi]; closeMenu(); pick(it); };
}
document.addEventListener("pointerdown", e => { if (menuEl && !e.target.closest(".kc-menu")) closeMenu(); }, true);
// "+ Add Step": every block with a title and one line; the ends under "End the run"; v1 nodes last
function addMenu(x, y){
  const all = Object.values(S.blocks), one = b => ({label:b.icon + " " + b.label, sub:DESC[b.b] || (b.group === "Legacy" ? "A KH 6 node: " + ((S.nodes[b.b] || {}).doc || b.label) : b.group), b:b.b});
  const groups = [...new Set(all.filter(b => !["Ends", "Legacy"].includes(b.group)).map(b => b.group))];
  const items = [{head:"Add a step"}, ...groups.flatMap(g => all.filter(b => b.group === g).map(one)),
    {head:"End the run"}, ...all.filter(b => b.group === "Ends").map(b => ({...one(b), label:b.icon + " " + (b.b === "end.return" ? "Return" : "Stop")})),
    {head:"KH 6 nodes (legacy)"}, ...all.filter(b => b.group === "Legacy").map(one)];
  menu(x, y, items, it => { const n = newStep(it.b); if (isEnd(n)) { placeEnd(n); render(); openEnd(n.id); } else { placeNew(n); render(); openStep(n.id); } }, true);
}
// drag a flow of the list onto the canvas: it runs there as one step
function listDrag(e, id){
  if (e.button !== 0) return; const sx = e.clientX, sy = e.clientY; let ghost = null;
  const move = ev => { if (!ghost) { if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 8) return; ghost = document.createElement("div"); ghost.className = "kc-ghost"; ghost.textContent = "⧉ " + ((S.fcache[id] || {}).name || id); document.body.appendChild(ghost); document.body.classList.add("kc-dragging"); }
    ev.preventDefault(); ghost.style.left = ev.clientX + "px"; ghost.style.top = ev.clientY + "px"; const c = $("ffCanvas"); if (c) { const el = document.elementFromPoint(ev.clientX, ev.clientY); c.classList.toggle("over", !!(el && el.closest("#ffCanvas"))); } };
  const up = ev => { document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", up); if (!ghost) return;
    ghost.remove(); document.body.classList.remove("kc-dragging"); const c = $("ffCanvas"); if (c) c.classList.remove("over");
    S.justDragged = true; setTimeout(() => { S.justDragged = false; }, 0);
    const el = document.elementFromPoint(ev.clientX, ev.clientY); if (!(el && el.closest("#ffCanvas")) || !fd()) return;
    if (S.v1) return toast("A v1 flow is read-only here.");
    if (id === S.fid) return toast("A flow cannot run itself.");
    const r = $("ffCv").getBoundingClientRect(), n = newStep("flow.call", {flow:id}); S.sel = null; placeNew(n);
    n.ui = {x:Math.max(0, Math.round((ev.clientX - r.left - NW / 2) / 10) * 10), y:Math.max(16, Math.round((ev.clientY - r.top - NH / 2) / 10) * 10)};
    render(); toast("“" + ((S.fcache[id] || {}).name || id) + "” runs here as one step. Wire its ✓ and ✕ dots."); };
  document.addEventListener("pointermove", move); document.addEventListener("pointerup", up);
}

// ── the step modal (K25): Identity (name, included in the final response), Settings (the block's own), read-only
// notes; the footer names what is wrong. Links between nodes are drawn on the canvas, never set here.
let M = null;
// the steps with a path to `id` (✓ or ✕ arrows), nearest first: what a node can read
function upstream(id){ const d = fd(), seen = new Set(), order = [], todo = [id];
  while (todo.length) { const t = todo.shift(); d.steps.forEach(y => { if (!seen.has(y.id) && y.id !== id && y.next && (y.next.ok === t || y.next.fail === t)) { seen.add(y.id); order.push(y); todo.push(y.id); } }); }
  return order.filter(y => !isEnd(y)); }
function openStep(id){ const x = fstep(id); if (!x) return; M = {id, s:clone(x), more:false}; renderStep(); }
function spaceText(v){ const c = CTX_SPACES.find(z => z.v === v); return c ? c.l : v ? "space “" + v + "”" + (S.gone[v] ? " (does not exist: pick another)" : "") : ""; }
// kh.call: the options that belong to the chosen action sit on one line right under "What to do" (spec op-options-line)
const OPT_LINE = {search:[["limit", "Top K"], ["floor", "Min match"]], rows:[["limit", "At most"]], newest:[["limit", "At most"]], walk:[["depth", "Steps"]], gaps:[["limit", "At most"]], "move overflow":[["limit", "Keep at most"]]};
const OPT_HINT = {gaps:"At most: the N newest open gaps.", "move overflow":"Keep at most: the hot log keeps this many rows; the oldest ones over it go to the logs of their space.", search:"Top K: the K rows closest in meaning; Min match: rows below it are dropped.", rows:"At most: the first N rows.", newest:"At most: the N newest rows.", walk:"Steps: how many links deep it follows (1 or 2)."};
const FLOORS = [["", "KH default"], ["0.45", "0.45"], ["0.5", "0.5"], ["0.6", "0.6"], ["0.7", "0.7"]];
// the text a Call KH works on: where it comes from (spec text-value); a text of one's own is a template
const VALUES = [["", "the prompt"], ["${input.part}", "input.part"], ["${input.tool_name}", "input.tool_name"], ["${input.tool_input}", "input.tool_input"]];
const BIND_SC = "@binding";
// ── outputs: what a node hands on (GET /flows/nodes: outputs, op_outputs), as the output picker and the When builder list them
const KIND_L = {text:"text", num:"a number", count:"a count", list:"a list of rows"};
const EXTRA_KIND = {groups:"list", leading_group:"list", core:"text", links:"list"};
function outsOf(x){
  if (x.b === "flow.call") return [["items", "list"], ["count", "count"]];
  const b = S.blocks[x.b] || {}, all = b.outputs || [["text", "text"]];
  if (x.b === "kh.call" && b.op_outputs) { const kinds = Object.fromEntries(all); return (b.op_outputs[x.args.op || "rows"] || ["items"]).map(f => [f, kinds[f] || EXTRA_KIND[f] || "text"]); }
  return all;
}
// for: "polars" (an expression reads steps["Name"]) or "template" (a text reads ${steps.<id>.x}; the caller is not offered there)
function outputsFor(id, forWhat){
  const ups = upstream(id).flatMap(z => outsOf(z).map(([f, t]) => ({src:stepName(z), sid:z.id, f, t})));
  const ps = (S.pseudo || []).filter(g => forWhat !== "template" || g.src !== "Caller").flatMap(g => g.fields.map(([f, t]) => ({src:g.src, f, t, pseudo:true})));
  return ups.concat(ps);
}
function refText(c, forWhat){
  if (forWhat === "template") return c.pseudo ? (c.src === "Prompt" ? "${input.text}" : "${input." + c.f + "}") : "${steps." + c.sid + "." + (c.f === "items" || c.f === "text" ? c.f : "fields." + c.f) + "}";
  if (c.pseudo) return c.src === "Prompt" ? "prompt" : (c.src === "Payload" ? "input" : "caller") + '["' + c.f + '"]';
  return 'steps["' + c.src + '"]' + (c.f === "items" ? "" : '["' + c.f + '"]');
}
// a template as people read it: ${steps.s3.items} -> {Read Rows.items}
const readable = t => String(t || "").replace(/\$\{steps\.([A-Za-z0-9_\-]+)\.(fields\.)?([A-Za-z0-9_\-]+)\}/g, (m, id, _f, f) => "{" + ((fstep(id) || {}).name || id) + "." + f + "}").replace(/\$\{input\.text\}/g, "{Prompt.text}").replace(/\$\{input\.([A-Za-z0-9_\-]+)\}/g, "{Payload.$1}");
// Which output? A growing list: a search box and 10 at a time, nearest node first, then Payload / Prompt / Caller.
function pickOutput(id, forWhat, done){ PK = {q:"", max:10, done, all:outputsFor(id, forWhat), out:true}; drawOutputPick(); }
function drawOutputPick(){
  const o = PK; if (!o) return; const q = o.q.trim().toLowerCase(), hits = o.all.filter(x => !q || (x.src + " " + x.f + " " + KIND_L[x.t]).toLowerCase().includes(q)), shown = hits.slice(0, o.max);
  const el = pickHost("output");
  el.innerHTML = '<div class="kc-modal" style="width:min(560px,100%)"><div class="kc-mhd"><span class="ttl">Which output?</span><span class="sp"></span><button class="xb" data-pkx title="Close (Esc)">✕</button></div><div class="kc-mbody">'
    + '<input type="text" class="kc-search" data-poq placeholder="Search ' + o.all.length + ' outputs by node or name" value="' + esc(o.q) + '">'
    + '<div class="kc-pick" id="kcPoList">' + shown.map(x => '<div data-pov="' + o.all.indexOf(x) + '"><b>' + esc(x.src) + ' · ' + esc(x.f) + '</b><small>' + esc((KIND_L[x.t] || x.t) + (x.pseudo ? " · given with the call" : " · output of this node; empty when the run did not pass it")) + '</small></div>').join("") + '</div>'
    + (hits.length > shown.length ? '<button class="btn g s" data-pom style="align-self:flex-start">Show 10 more (' + shown.length + ' of ' + hits.length + ')</button>' : '')
    + (hits.length ? '' : '<p class="kc-hint">No output matches.</p>')
    + (o.all.some(x => !x.pseudo) ? '' : '<p class="kc-hint">No node leads here yet: draw an arrow into this node to use its outputs.</p>')
    + '</div><div class="kc-mft"><span class="iss"></span><span style="display:flex;gap:10px"><button class="btn g s" data-pkx>Cancel</button></span></div></div>';
  el.querySelectorAll("[data-pkx]").forEach(b => b.onclick = closePick);
  const qi = el.querySelector("[data-poq]"); qi.oninput = () => { o.q = qi.value; o.max = 10; drawOutputPick(); const n = document.querySelector("[data-poq]"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } };
  if (o.focus !== false) setTimeout(() => { const n = document.querySelector("[data-poq]"); if (n) n.focus(); }, 0);
  const m = el.querySelector("[data-pom]"); if (m) m.onclick = () => { o.max += 10; o.focus = false; drawOutputPick(); };
  el.querySelectorAll("[data-pov]").forEach(dv => dv.onclick = () => { const x = o.all[+dv.dataset.pov], f = o.done; closePick(); f(x); });
}
// ── Polars checks: POST /flows/expr/check, cached by kind + text + what the node can read, so the canvas and the
// issue list show the same red as the field
const exprKey = (kind, text, ups) => kind + "|" + text + "|" + ups.join("\u0001");
function exprErr(x, k){ const v = String(x.args[k] || "").trim(); if (!v) return ""; const kind = k === "cond" ? "when" : "filter", ups = upstream(x.id).map(z => z.name);
  const r = S.expr[exprKey(kind, v, ups)]; if (!r || r.ok) return "";
  const lbl = k === "cond" ? "“When”" : "“Custom filter”"; return /^reads "/.test(r.error) ? lbl + " " + r.error : lbl + " must be a Polars expression: " + r.error; }
async function checkExpr(x, k){ const v = String(x.args[k] || "").trim(); if (!v) return; const kind = k === "cond" ? "when" : "filter", ups = upstream(x.id).map(z => z.name), key = exprKey(kind, v, ups);
  if (S.expr[key]) return; try { S.expr[key] = await api("POST", "/flows/expr/check", {expr:v, kind, upstream:ups}); } catch(e) {} }
// the store of a scope: the space's own frame for a named space, the template's default for a context space
function storeIn(space, scope){ if (!scope || scope === BIND_SC) return null; if (space && space[0] !== "@") return (S.stores[space] || {})[scope] || null; return DEF_STORE[scope] || null; }
function field(s, p){
  const v = s.args[p.k], k = p.k, ro = S.v1 ? " disabled" : "";
  const opt = (list, cur) => list.map(o => { const [val, l] = Array.isArray(o) ? o : [o, o]; return '<option value="' + esc(val) + '"' + (String(val) === String(cur ?? "") ? " selected" : "") + '>' + esc(l) + '</option>'; }).join("");
  if (s.b === "kh.call" && k === "op") {
    const line = (OPT_LINE[v || "rows"] || []).map(([kk, l]) => '<label class="kc-inl-l">' + l + (kk === "floor" ? '<select data-k="floor"' + ro + '>' + opt(FLOORS, s.args.floor ?? "") + '</select>'
      : '<input type="number" min="1"' + (kk === "depth" ? ' max="2"' : '') + ' data-k="' + kk + '"' + ro + ' value="' + esc(s.args[kk] ?? "") + '">') + '</label>').join("");
    return row("What to do", '<select data-k="op"' + ro + '>' + opt((p.opts || []).map(o => [o, (KH_OPS[o] || o) + (KH_OPD[o] ? " · " + KH_OPD[o] : "")]), v) + '</select>' + (line ? '<div class="kc-inl" id="kcOptLine">' + line + '</div>' : ''),
      OPT_HINT[v] || esc(KH_OPD[v] || "")); }
  if (s.b === "kh.call" && k === "text") {
    const own = M.ownText || (v && !VALUES.some(([x]) => x === v));
    return row("Text", '<select data-tv' + ro + '>' + opt(VALUES.concat([["__own", "a text of my own…"]]), own ? "__own" : (v || "")) + '</select>'
      + (own ? '<div style="margin-top:6px">' + tmplArea(s, k, "e.g. ${steps.s1.items}") + '</div>' : ''), own ? "A text of your own; + Insert an output puts an earlier node's output in it." : "Where the text comes from when the flow runs."); }
  if (s.b === "agent.task" && k === "shape") { const t = String(v || "").trim(), bad = shapeIssue(t);
    return row(p.label, '<textarea data-k="shape" class="kc-code" rows="3" spellcheck="false"' + ro + ' placeholder="{&quot;summary&quot;: &quot;text&quot;}">' + esc(v ?? "") + '</textarea>'
      + '<div class="kc-chk' + (bad ? " bad" : t ? " ok" : "") + '" data-shchk>' + (bad ? "✕ " + esc(bad) : t ? (/^[\[{]/.test(t) ? "✓ JSON: the answer is checked against this shape" : "Plain words: the agent reads it as a guide") : "Empty: any answer goes on") + '</div>',
      "The shape the agent's answer must have, e.g. {\"summary\": \"text\"}. JSON is checked here."); }
  if (s.b === "agent.task" && k === "guide") return row(p.label, '<textarea data-k="guide"' + ro + ' placeholder="e.g. Answer in three lines; quote ids as they are.">' + esc(v ?? "") + '</textarea>', "What the agent should keep to while it answers.");
  switch (p.kind) {
    case "choice": return row(p.label, '<select data-k="' + k + '"' + ro + '>' + opt((p.opts || []).map(o => [o, o || "(none)"]), v) + '</select>');
    case "space": case "project": return row(p.label, pickField("pksp=\"" + k + "\"", spaceText(v), p.kind === "project" ? "Pick a project…" : "Pick a space…"), "Opens the space picker: a space from the context, or search the spaces you can read.");
    case "scope": { const sp = s.args.space, named = sp && sp[0] !== "@", l = named ? (S.scopes[sp] || []) : Object.keys(SCOPE_LABEL).concat(SYS_IDS);
      const all = l.concat(v && v !== BIND_SC && !l.includes(v) ? [v] : []);
      return row(p.label, '<select data-k="scope"' + ro + '><option value="">(pick one)</option><option value="' + BIND_SC + '"' + (v === BIND_SC ? " selected" : "") + '>Binding Scopes · the scopes given in the payload</option>'
        + opt(all.map(x => [x, (SCOPE_LABEL[x] || x) + (storeIn(sp, x) ? " · " + (STORE_L[storeIn(sp, x)] || storeIn(sp, x)) : "") + (l.includes(x) ? "" : " (not in this space)")]), v) + '</select>',
        v === BIND_SC ? 'Runs on each scope the caller names in the payload, e.g. {"scopes": ["knowledges", "findings"]}; one the space lacks or the caller cannot read is skipped.'
          : named ? "The scopes of " + esc(sp) + " you can read." : "Any scope; the step fails when the space has no such scope."); }
    case "flow": return row(p.label, pickField("pkfl", (S.fcache[v] || {}).name || v || "", "Pick a flow…"), "Opens the flow picker: search, filter, 10 at a time.");
    case "bool": return row(p.label, '<span class="kc-tgl' + ((v ?? p.def) ? " on" : "") + '" data-bool="' + k + '"><i></i>' + ((v ?? p.def) ? "yes" : "no") + '</span>');
    case "int": case "number": return row(p.label, '<input type="number" data-k="' + k + '"' + ro + ' value="' + esc(v ?? "") + '"' + (p.kind === "number" ? ' step="0.01"' : '') + '>');
    case "step": { const ups = upstream(s.id); return row(p.label, '<select data-k="' + k + '"' + ro + '><option value="">(the step before)</option>' + opt(ups.map(z => [z.name, z.name]), v) + '</select>', "Only steps with a path to this one."); }
    case "steps": { const ups = upstream(s.id), cur = Array.isArray(v) ? v : [];
      return row(p.label, '<div class="kc-ins">' + (ups.map(z => '<label><input type="checkbox" data-in="' + esc(z.name) + '"' + (cur.includes(z.name) ? " checked" : "") + ro + '>' + esc(z.name) + '</label>').join("") || '<span class="kc-hint">No step leads here yet: draw an arrow into this node first.</span>') + '</div>'); }
    case "polars": { if (k === "cond" && s.b === "logic.decide") return row(p.label, condBuilder(s, p), condMode(s.args) === "polars"
        ? "A Polars expression giving one true or false: steps[\"Node name\"], input, prompt, caller. True: the yes exit; false, or an error: the no exit."
        : "Each line: an output, a test, a target; Then joins it to the next line (and before or, as in Jira). True: the yes exit; false, or an error: the no exit.");
      const ups = upstream(s.id), err = exprErr(s, k);
      return row(p.label, '<div class="cb-wrap"><button type="button" class="cb-add cb-add-top" data-insout="' + k + '" data-for="polars">+ Insert an output</button><textarea data-k="' + k + '" class="kc-code cb-poltxt" rows="2" spellcheck="false"' + ro + ' placeholder="' + (k === "cond" ? 'steps[&quot;Node name&quot;][&quot;status&quot;][0] == &quot;missing&quot;' : 'pl.col(&quot;status&quot;) == &quot;open&quot;') + '">' + esc(v ?? "") + '</textarea></div>'
        + '<div class="kc-err" data-err="' + k + '">' + esc(err) + '</div>'
        + '<div class="kc-reads"><span>Can read:</span>' + (ups.length ? ups.map(z => '<button type="button" data-stp="' + esc(z.name) + '" data-for="' + k + '">' + esc(z.name) + '</button>').join("") : '<i>no node leads here yet: draw an arrow into this node first</i>') + '</div>',
        k === "cond" ? "A Polars expression giving one true or false. True: the yes exit; false or an error: the no exit." : "A Polars expression over the scope's rows, e.g. pl.col(\"status\") == \"open\". It can also read input, prompt, caller and steps[\"Node name\"]. Empty: every row."); }
    case "template": case "prompt": if (s.b === "text.format" && k === "row") return row(p.label, '<input type="text" data-k="row"' + ro + ' value="' + esc(v ?? "") + '">', "{text} is the row's text, e.g. “- {text}”.");
      return row(p.label, tmplArea(s, k, ""), "+ Insert an output puts the output of an earlier node (or the payload, the prompt) where the cursor is.");
    default: return row(p.label, '<input type="text" data-k="' + k + '"' + ro + ' value="' + esc(Array.isArray(v) ? v.join(", ") : v ?? "") + '">');
  }
}
// a template text: a textarea, '+ Insert an output' and the text as people read it ({Node.output})
function tmplArea(s, k, ph){ const v = s.args[k] ?? "", r = readable(v);
  return '<div class="cb-wrap"><button type="button" class="cb-add cb-add-top" data-insout="' + k + '" data-for="template"' + (S.v1 ? " disabled" : "") + '>+ Insert an output</button><textarea data-k="' + k + '" class="cb-poltxt" rows="3"' + (S.v1 ? " disabled" : "") + (ph ? ' placeholder="' + esc(ph) + '"' : '') + '>' + esc(v) + '</textarea></div>'
    + '<div class="kc-hint kc-reads-as" data-ras="' + k + '">' + (r !== v ? "Reads as: " + esc(r) : "") + '</div>'; }
function shapeIssue(t){ if (!t || !/^[\[{]/.test(t)) return ""; try { JSON.parse(t); return ""; } catch(e) { return "Not valid JSON: " + e.message; } }
function renderStep(){
  if (!M) return; const s = M.s, b = blk(s.b), bad = problem(s);
  const include = row("", '<span class="kc-tgl' + (s.hidden ? "" : " on") + '" data-sh><i></i>' + (s.hidden ? "Not in the final response (only later steps use it)" : "Included in the final response, under the node name") + '</span>');
  let main = [], more = [];
  const op = s.args.op || "rows", keys = s.b === "kh.call" ? (KH_MAIN[op] || []) : null, inLine = (OPT_LINE[op] || []).map(z => z[0]);
  (b.params || []).forEach(p => { if (s.b === "flow.call") return; if (s.b === "kh.call" && (p.k === "op" || inLine.includes(p.k))) return;
    if (keys && !keys.includes(p.k)) return;            // a Call KH shows only what its op uses
    main.push(field(s, p)); });
  if (s.b === "kh.call") main.unshift(field(s, b.params.find(p => p.k === "op")));
  const note = s.b === "agent.task" ? '<p class="kc-note">The agent running the flow answers this; the run waits. KH calls no LLM itself: the run stops at this node until the agent sends its answer, then goes on with it as this node\'s output.</p>'
    : '<p class="kc-note">' + esc(DESC[s.b] || b.label) + '</p>';
  const body = area("Identity", row("Name of this node", '<input type="text" data-name' + (S.v1 ? " disabled" : "") + ' placeholder="' + esc(b.label) + '" value="' + esc(s.name || "") + '">', "Unique in the flow: Polars expressions read steps[\"" + esc(s.name || "") + "\"].") + include)
    + area("Settings", s.b === "flow.call" ? row("Runs the flow", pickField("pkfl", (S.fcache[s.args.flow] || {}).name || s.args.flow || "", "Pick a flow…"), "Opens the flow picker. Or close this and drag a flow from Existing Flows onto the canvas.")
        : (main.join("") || '<p class="kc-hint">Nothing to set: it always does the same.</p>') + (more.length ? '<details class="kc-more"' + (M.more ? " open" : "") + '><summary>More options</summary>' + more.join("") + '</details>' : ''))
    + area("Note", note + '<p class="kc-note">Links between nodes are drawn on the canvas, never set here: its exits (✓ works, ✕ fails' + (isDecide(s) ? "; yes / no for Decide" : "") + ') are its dots.' + (s.when ? ' v1: runs only if ' + esc(s.when) + ' is set.' : '') + '</p><p class="kc-hint kc-mono">' + esc(s.b) + ' · step id ' + esc(s.id) + '</p>');
  const el = shell('<span class="ttl">' + (s.b === "flow.call" ? "⧉" : b.icon) + ' ' + esc(stepName(s)) + '</span><span class="kc-sub">' + esc(b.label) + '</span>', body,
    '<span class="iss" id="kcIss">' + (bad ? "⚠ " + esc(bad) : "") + '</span><span style="display:flex;gap:10px">' + (s.b === "flow.call" && s.args.flow ? '<button class="btn g s" data-open>Open this flow</button>' : '')
    + '<button class="btn g s" data-x>Cancel</button>' + (S.v1 ? '' : '<button class="btn s" data-apply>Apply</button>') + '</span>');
  el.querySelector(".kc-modal").style.width = "min(860px,100%)";
  const q = sel => el.querySelector(sel), all = sel => el.querySelectorAll(sel);
  const foot = () => { const w = problem(s), i = $("kcIss"); if (i) i.textContent = w ? "⚠ " + w : ""; };
  const ap = q("[data-apply]"); if (ap) ap.onclick = () => { const d = fd(); if (d.steps.some(z => z.id !== s.id && z.name === s.name)) return toast("Another step is named “" + s.name + "”: a name is unique in the flow.");
    const k = d.steps.findIndex(z => z.id === M.id); d.steps[k] = clone(s); M = null; closeShell(); render(); toast("Applied. Save the flow to keep it."); };
  const opn = q("[data-open]"); if (opn) opn.onclick = () => { if (fdirty() && !confirm("Leave “" + fd().name + "” without saving?")) return; const to = s.args.flow, back = S.fid; M = null; closeShell(); openFlow(to, back); };
  const det = q("details.kc-more"); if (det) det.ontoggle = () => { M.more = det.open; };
  if (S.v1) return;
  q("[data-name]").oninput = e => { s.name = e.target.value.trim(); };
  q("[data-sh]").onclick = () => { if (s.hidden) delete s.hidden; else s.hidden = true; renderStep(); };
  // a Polars field is checked by KH while one types (POST /flows/expr/check); the red line and the footer follow
  const live = (k) => { clearTimeout(M.ct); M.ct = setTimeout(async () => { await checkExpr(s, k); if (!M || M.s !== s) return; const e = q('[data-err="' + k + '"]'); if (e) e.textContent = exprErr(s, k); foot(); }, 300); };
  all("[data-k]").forEach(i => { const p = b.params.find(z => z.k === i.dataset.k); if (!p) return;
    const set = () => { if (i.value === "") delete s.args[p.k]; else s.args[p.k] = (p.kind === "int" ? parseInt(i.value, 10) : p.kind === "number" ? parseFloat(i.value) : i.value); };
    if (i.tagName === "SELECT") i.onchange = () => { set(); if (p.k === "op") delete M.ownText; renderStep(); };
    else i.oninput = () => { set(); if (p.kind === "polars") live(p.k); if (p.k === "shape") { const c = q("[data-shchk]"), t = String(s.args.shape || "").trim(), bd = shapeIssue(t);
        c.className = "kc-chk" + (bd ? " bad" : t ? " ok" : ""); c.textContent = bd ? "✕ " + bd : t ? (/^[\[{]/.test(t) ? "✓ JSON: the answer is checked against this shape" : "Plain words: the agent reads it as a guide") : "Empty: any answer goes on"; }
      const ra = q('[data-ras="' + p.k + '"]'); if (ra) { const r = readable(i.value); ra.textContent = r !== i.value ? "Reads as: " + r : ""; } foot(); }; });
  ["where", "cond"].forEach(k => { if (s.args[k] && !(k === "cond" && s.b === "logic.decide" && condMode(s.args) === "chips")) live(k); });
  const tv = q("[data-tv]"); if (tv) tv.onchange = () => { if (tv.value === "__own") { M.ownText = true; if (VALUES.some(([x]) => x === (s.args.text || ""))) delete s.args.text; } else { delete M.ownText; if (tv.value) s.args.text = tv.value; else delete s.args.text; } renderStep(); };
  all("[data-bool]").forEach(t => t.onclick = () => { const p = b.params.find(z => z.k === t.dataset.bool); s.args[p.k] = !(s.args[p.k] ?? p.def); renderStep(); });
  all("[data-in]").forEach(c => c.onchange = () => { const k = (b.params.find(z => z.kind === "steps") || {}).k; const cur = Array.isArray(s.args[k]) ? s.args[k] : []; s.args[k] = c.checked ? [...cur, c.dataset.in] : cur.filter(z => z !== c.dataset.in); });
  const insertAt = (t, ins) => { const a = t.selectionStart ?? t.value.length; t.value = t.value.slice(0, a) + ins + t.value.slice(t.selectionEnd ?? a); t.focus(); t.selectionStart = t.selectionEnd = a + ins.length; t.dispatchEvent(new Event("input")); };
  all("[data-stp]").forEach(c => c.onclick = () => insertAt(q('[data-k="' + c.dataset.for + '"]'), 'steps["' + c.dataset.stp + '"]'));
  // '+ Insert an output': the output picker, then the reference goes where the cursor was
  all("[data-insout]").forEach(c => { const t = q('textarea[data-k="' + c.dataset.insout + '"]'); if (t) { const keep = () => { t.dataset.a = t.selectionStart; t.dataset.e = t.selectionEnd; }; t.onkeyup = keep; t.onmouseup = keep; t.onblur = keep; }
    c.onclick = () => pickOutput(s.id, c.dataset.for, x => { const t2 = q('textarea[data-k="' + c.dataset.insout + '"]'); if (!t2) return; if (t2.dataset.a !== undefined) { t2.selectionStart = +t2.dataset.a; t2.selectionEnd = +t2.dataset.e; } else t2.selectionStart = t2.selectionEnd = t2.value.length; insertAt(t2, refText(x, c.dataset.for)); }); });
  all("[data-pksp]").forEach(btn => btn.onclick = () => { const k = btn.dataset.pksp; pickSpace(s.args[k], v => { s.args[k] = v; loadScopes(v).then(renderStep); }); });
  all("[data-pkfl]").forEach(btn => btn.onclick = () => pickFlow({title:"Which flow does this step run?", chosen:s.args.flow, skip:id => id === S.fid,
    onDone:id => { s.args.flow = id; if (!s.name || s.name === "Flow" || /^Flow( \d+)?$/.test(s.name)) s.name = uniqName({steps:fd().steps.filter(z => z.id !== s.id)}, (S.fcache[id] || {}).name || id); renderStep(); }}));
  if (s.b === "logic.decide") wireCondBuilder(q, all, s);
  if (s.args.space && s.args.space[0] !== "@" && !S.scopes[s.args.space]) loadScopes(s.args.space).then(() => { if (M && M.s === s) renderStep(); });
}
// the scopes of a real space and their stores (GET /spaces/{space}/scopes, only those the caller reads); a space KH
// does not know (deleted, or hidden from the caller) is remembered, so the node and the footer say so
async function loadScopes(sp){ if (!sp || sp[0] === "@" || S.scopes[sp]) return;
  try { const r = await api("GET", "/spaces/" + encodeURIComponent(sp) + "/scopes"); S.scopes[sp] = r.scopes.map(x => x.scope); S.stores[sp] = Object.fromEntries(r.scopes.map(x => [x.scope, x.store])); delete S.gone[sp]; }
  catch(e) { S.scopes[sp] = []; S.stores[sp] = {}; if (e.status === 404) S.gone[sp] = true; } }

// ── the When builder of Decide (K26): Jira-like lines (output, test, target, then and/or) compiled to one Polars
// expression shown under the box; Chips | Polars switches to the text for what the lines cannot say. The lines are
// kept in args.rules (args.mode says which one is in charge); KH runs args.cond, the compiled text.
const IS_KINDS = {text:["null", "empty", "text"], num:["null", "numeric"], list:["empty"]};
const opsBy = t => (S.opsBy || {})[t] || ["equals"];
const lit = v => /^-?\d+(\.\d+)?$/.test(String(v).trim()) ? String(v).trim() : JSON.stringify(String(v));
function refOf(r){ const P = r.src === "Payload", C = r.src === "Caller";
  if (r.src === "Prompt") return {col:"pl.Series([prompt])"};
  if (P || C) { const v = (P ? "input" : "caller") + '["' + r.f + '"]'; return r.t === "list" ? {len:"pl.Series(" + v + ").len()", col:"pl.Series(" + v + ")"} : {col:"pl.Series([" + v + "])"}; }
  const s = 'steps["' + r.src + '"]';
  if (r.f === "items") return {len:s + ".height"};
  if (r.t === "count") return {num:(r.f === "count" ? s + ".height" : s + '["' + r.f + '"][0]')};
  return r.t === "list" ? {len:s + '["' + r.f + '"].list.len()[0]', col:s + '["' + r.f + '"]'} : {col:s + '["' + r.f + '"]'}; }
function compileRule(r){ const x = refOf(r), C = x.col, V = lit(r.v ?? ""), cmp = {"equals":"==", "not equals":"!=", ">":">", ">=":">=", "<":"<", "<=":"<="};
  if (r.t === "count") return "(" + x.num + " " + cmp[r.op] + " " + V + ")";
  if (r.t === "list") { if (r.op === "is" || r.op === "is not") return "(" + x.len + (r.op === "is" ? " == 0" : " > 0") + ")";
    return "(" + x.len + " " + {"count equals":"==", "count >":">", "count <":"<"}[r.op] + " " + V + ")"; }
  if (r.op === "is" || r.op === "is not") { const e = {null:C + ".is_null().all()", empty:"(" + C + ".str.len_chars() == 0).all()", text:"(" + C + ".dtype == pl.String)", numeric:C + ".dtype.is_numeric()"}[r.v || "null"];
    if (r.op === "is not" && (r.v || "null") === "null") return "(" + C + ".is_not_null().all())";
    return r.op === "is" ? "(" + e + ")" : "(not " + e + ")"; }
  if (r.op === "contains") return "(" + C + ".str.contains(" + JSON.stringify(String(r.v ?? "")) + ", literal=True).all())";
  return "(" + C + " " + cmp[r.op] + " " + V + ").all()"; }
// a line's and/or joins it to the next line; Python's & binds before |, so 'and' binds before 'or' as in Jira
const compileRules = rules => rules.map((r, i) => (i ? ((rules[i - 1].next || "and") === "or" ? " | " : " & ") : "") + compileRule(r)).join("");
const ruleIssue = r => r.op && !["is", "is not"].includes(r.op) && String(r.v ?? "").trim() === "" ? "give the target of “" + r.src + " · " + r.f + "”" : "";
function newRule(c){ const t = c.t, op = opsBy(t)[0]; return {next:"and", src:c.src, f:c.f, t, op, v:["is", "is not"].includes(op) ? (IS_KINDS[t] || ["null"])[0] : ""}; }
const condMode = a => a.mode || (a.rules ? "chips" : a.cond ? "polars" : "chips");
function condBuilder(s, p){
  const a = s.args, rules = a.rules || [], polars = condMode(a) === "polars", ro = S.v1 ? " disabled" : "";
  const tg = '<div class="cb-tgl" title="Chips: build it from lines · Polars: write it as text">' + [["chips", "Chips"], ["polars", "Polars"]].map(([k, l]) => '<button type="button" data-cbmode="' + k + '"' + ((polars ? "polars" : "chips") === k ? ' class="on"' : '') + ro + '>' + l + '</button>').join("") + '</div>';
  if (polars) return '<div class="cb-wrap">' + tg + '<button type="button" class="cb-add cb-add-top" data-cbadd' + ro + '>+ Insert an output</button><textarea data-k="' + p.k + '" class="kc-code cb-poltxt" rows="4" spellcheck="false"' + ro + '>' + esc(a.cond || "") + '</textarea></div>'
    + '<div class="kc-err" data-err="cond">' + esc(exprErr(s, "cond")) + '</div>';
  const sel = (i, k, list, v) => '<select data-cbr="' + i + '" data-cbk="' + k + '"' + ro + '>' + list.map(o => '<option' + (o === v ? " selected" : "") + '>' + esc(o) + '</option>').join("") + '</select>';
  const line = (r, i) => '<div class="cb-row" data-cbrow="' + i + '">'
    + '<div class="cb-c1"><span class="cb-chip on" data-cbslot="' + i + '" title="' + esc(r.src + ' · ' + r.f) + ': click to pick another output">' + esc(r.src) + ' <b>·</b> ' + esc(r.f) + '</span></div>'
    + '<div>' + sel(i, "op", opsBy(r.t), r.op) + '</div>'
    + '<div>' + (["is", "is not"].includes(r.op) ? sel(i, "v", IS_KINDS[r.t] || ["null"], r.v) : '<input type="text" data-cbr="' + i + '" data-cbk="v" placeholder="value" value="' + esc(r.v ?? "") + '"' + ro + '>') + '</div>'
    + '<div>' + (i < rules.length - 1 ? sel(i, "next", ["and", "or"], r.next || "and") : '') + '</div>'
    + '<div><button type="button" class="cb-x" data-cbdel="' + i + '" title="Remove this condition"' + ro + '>✕</button></div></div>';
  return '<div class="cb-wrap">' + tg + '<div class="cb-box">' + (rules.length ? '<div class="cb-row cb-head"><div>Output</div><div>Operation</div><div>Target</div><div>Then</div><div></div></div>' + rules.map(line).join("") : '<p class="cb-empty">No condition yet.</p>')
    + '<button type="button" class="cb-add" data-cbadd' + ro + '>+ Add condition</button></div></div>'
    + '<div class="cb-out"><span>Polars</span><code data-cbcode>' + esc(a.cond || "") + '</code></div><div class="kc-err" data-err="cond">' + esc(exprErr(s, "cond")) + '</div>';
}
function wireCondBuilder(q, all, s){
  const a = s.args, rules = () => (a.rules = a.rules || []);
  const recheck = async () => { await checkExpr(s, "cond"); if (!M || M.s !== s) return; const e = q('[data-err="cond"]'); if (e) e.textContent = exprErr(s, "cond"); const w = problem(s), i = $("kcIss"); if (i) i.textContent = w ? "⚠ " + w : ""; };
  const sync = () => { a.cond = compileRules(rules()); const c = q("[data-cbcode]"); if (c) c.textContent = a.cond; clearTimeout(M.ct); M.ct = setTimeout(recheck, 300); const w = problem(s), i = $("kcIss"); if (i) i.textContent = w ? "⚠ " + w : ""; };
  const redo = () => { a.cond = compileRules(rules()); a.mode = "chips"; renderStep(); };
  const add = (c, at) => { if (at == null) rules().push(newRule(c)); else { const r = rules()[at]; Object.assign(r, newRule(c), {next:r.next}); } redo(); };
  all("[data-cbadd]").forEach(b => b.onclick = () => pickOutput(s.id, "polars", c => { const t = q('textarea[data-k="cond"]');
    if (t) { const ins = refText(c, "polars"), at = t.dataset.a !== undefined ? +t.dataset.a : t.value.length, end = t.dataset.e !== undefined ? +t.dataset.e : at; a.cond = t.value.slice(0, at) + ins + t.value.slice(end); renderStep(); }
    else add(c); }));
  const t = q('textarea[data-k="cond"]'); if (t) { const keep = () => { t.dataset.a = t.selectionStart; t.dataset.e = t.selectionEnd; }; t.onkeyup = keep; t.onmouseup = keep; t.onblur = keep; }
  all("[data-cbslot]").forEach(x => x.onclick = () => pickOutput(s.id, "polars", c => add(c, +x.dataset.cbslot)));
  all("select[data-cbr]").forEach(x => x.onchange = () => { const r = rules()[+x.dataset.cbr]; r[x.dataset.cbk] = x.value;
    if (x.dataset.cbk === "op") r.v = ["is", "is not"].includes(r.op) ? (IS_KINDS[r.t] || ["null"])[0] : ((IS_KINDS[r.t] || []).includes(r.v) ? "" : r.v);
    redo(); });
  all("input[data-cbr]").forEach(x => x.oninput = () => { rules()[+x.dataset.cbr].v = x.value; sync(); });
  all("[data-cbdel]").forEach(x => x.onclick = () => { rules().splice(+x.dataset.cbdel, 1); redo(); });
  all("[data-cbmode]").forEach(x => x.onclick = () => { const to = x.dataset.cbmode; if (to === condMode(a)) return;
    const go = () => { a.mode = to; if (to === "chips") a.cond = compileRules(rules()); renderStep(); };
    if (to === "chips" && (a.cond || "") !== compileRules(a.rules || [])) return askSure("Switch to chips?", "The Polars text is replaced by what the chip lines say.", "Switch to chips", go);
    go(); });
  if (condMode(a) === "chips" && a.cond) recheck();
}
// ── ends
// What the agent really gets when a run ends in this Stop, for each way the flow starts (spec end-modals)
function agentGets(c){
  const d = fd(), msg = c.message ? "“" + c.message + "”" : "no message", why = c.why ? " (the log also keeps “" + c.why + "”)" : "", its = c.items ? " + the items so far" : "", out = [];
  if (c.outcome === "quiet") return ["Nothing: the run ends without an answer and without an error, whatever started it."];
  (d.triggers || []).forEach(t => { const e = EVENTS[t.event] || {};
    if (t.type === "prompt") out.push("Prompt trigger (the kh tool call): the tool result is " + (c.outcome === "deny" ? "a refusal, not an error: " + msg : "an error: " + msg + its) + ".");
    else if (e.hook === "PreToolUse") out.push(c.outcome === "deny" ? "PreToolUse hook: KH blocks the tool; Claude sees " + msg + " as the reason and does not run it." : "PreToolUse hook: the flow failed, so KH does not block the tool; Claude gets " + msg + " as context.");
    else if (e.hook === "UserPromptSubmit" && c.outcome === "deny") out.push("UserPromptSubmit hook: the prompt is blocked; the person sees " + msg + " as the reason.");
    else if (e.hook) out.push(e.hook + " hook: Claude gets " + msg + its + " as context; the session goes on (a KH flow never stops it)" + (c.outcome === "deny" ? "; this hook cannot block, so Deny acts as Failed." : "."));
    else if (t.event === "kh.agent_call") out.push("Agent calls it by name: the tool " + toolNameOf(S.fid) + " answers " + (c.outcome === "deny" ? "a refusal: " : "an error: ") + msg + its + "."); });
  const par = S.parentsWant || S.parents || []; if (par.length) out.push("As a ⧉ step in " + par.map(p => (S.fcache[p] || {}).name || p).join(", ") + ": that step fails and the parent goes on by its ✕ exit, carrying " + msg + ".");
  if (!out.length) out.push("This flow has no trigger and no flow runs it yet: nobody gets anything.");
  return out.map(t => t + why); }
function openEnd(id){
  const x = fstep(id), s = clone(x), stop = x.b === "end.stop", b = blk(x.b);
  const again = () => { const oc = s.args.outcome || "failed";
    const el = shell('<span class="ttl">' + b.icon + ' ' + esc(stepName(s)) + '</span><span class="kc-sub">' + (stop ? "ends this branch with an outcome of its own" : "ends this branch as worked") + '</span>',
      area("Identity", row("Name of this node", '<input type="text" data-name value="' + esc(s.name || "") + '"' + (S.v1 ? " disabled" : "") + '>'))
      + area("Settings", stop ? row("Outcome", '<div class="kc-pick">' + [["failed", "Failed", "The run failed: the caller gets an error"], ["deny", "Deny", "No on purpose: a hook blocks the tool or the prompt, the message is the reason"], ["quiet", "Stop quietly", "Ends with nothing handed back, not an error"]]
          .map(([k, l, dd]) => '<div data-oc="' + k + '" class="' + (oc === k ? "on" : "") + '"><b>' + l + '</b><small>' + dd + '</small></div>').join("") + '</div>')
          + (oc === "quiet" ? '' : row("Message to the agent", '<textarea data-msg placeholder="' + (oc === "deny" ? "e.g. This tool is blocked in this space: ask the owner." : "e.g. KH could not load the rules; answer without them and say so.") + '">' + esc(s.args.message || "") + '</textarea>')
            + row("Why (logged)", '<input type="text" data-why value="' + esc(s.args.why || "") + '">')
            + (oc === "failed" ? row("", '<span class="kc-tgl' + (s.args.items ? " on" : "") + '" data-its><i></i>' + (s.args.items ? "Also hand back the items so far" : "Hand back no items") + '</span>') : ''))
        : row("Text on top", '<textarea data-ld placeholder="e.g. No ticket matched; here is what KH found nearby.">' + esc(s.args.lead || "") + '</textarea>', "Hands back the items of the steps this run went through, written as set in the flow's 📦 Return Response (Item format…), with this text on top."))
      + (stop ? area("What the agent gets", '<div id="kcGets">' + agentGets(s.args).map(t => '<p class="kc-note" style="margin:0 0 6px">• ' + esc(t) + '</p>').join("") + '</div>') : '')
      + area("Note", '<p class="kc-note">' + esc(DESC[x.b]) + ' An end has no exits.</p>'),
      '<span class="iss"></span><span style="display:flex;gap:10px">' + (stop ? '' : '<button class="btn g s" data-fmt>Item format…</button>') + '<button class="btn g s" data-x>Cancel</button>' + (S.v1 ? '' : '<button class="btn s" data-ok>Apply</button>') + '</span>');
    const keep = () => { const n = el.querySelector("[data-name]"); if (n) s.name = n.value.trim() || s.name; const m = el.querySelector("[data-msg]"); if (m) s.args.message = m.value;
      const w = el.querySelector("[data-why]"); if (w) s.args.why = w.value; const l = el.querySelector("[data-ld]"); if (l) s.args.lead = l.value; };
    el.querySelectorAll("[data-oc]").forEach(dv => dv.onclick = () => { keep(); s.args.outcome = dv.dataset.oc; if (dv.dataset.oc !== "failed") delete s.args.items; again(); });
    const m = el.querySelector("[data-msg]"); if (m) m.oninput = () => { s.args.message = m.value; const g = $("kcGets"); if (g) g.innerHTML = agentGets(s.args).map(t => '<p class="kc-note" style="margin:0 0 6px">• ' + esc(t) + '</p>').join(""); };
    const it = el.querySelector("[data-its]"); if (it) it.onclick = () => { keep(); s.args.items = !s.args.items; again(); };
    const fm = el.querySelector("[data-fmt]"); if (fm) fm.onclick = () => { keep(); openAnswer(); };
    const ok = el.querySelector("[data-ok]"); if (ok) ok.onclick = () => { keep(); const d = fd(); if (d.steps.some(z => z.id !== s.id && z.name === s.name)) return toast("Another step is named “" + s.name + "”.");
      d.steps[d.steps.findIndex(z => z.id === id)] = s; closeShell(); render(); toast("Applied. Save the flow to keep it."); }; };
  again();
}
function openFail(){
  shell('<span class="ttl">⛔ Stop: failed</span><span class="kc-sub">the default end of every ✕ exit not wired elsewhere</span>',
    area("Identity", row("Name", '<input type="text" value="Stop: failed" disabled>'))
    + area("Note", '<p class="kc-note">The run ends as failed and the caller gets the reason: “step &lt;name&gt; failed: &lt;why&gt;”. A failed step never counts as done: unless its ✕ exit is wired somewhere else, the flow stops here. For an outcome (deny, quiet) or a message of your own, + Add Step › End the run › Stop.</p>'),
    '<span class="iss"></span><button class="btn g s" data-x>Close</button>');
}
// Return Response: the items of the steps the run passed through, in that order; a hook gets them in parts.
function openAnswer(){
  const d = fd(), a = clone(d.answer || {row:"{text}", part:9000}), hid = {}; d.steps.filter(x => !isEnd(x)).forEach(x => hid[x.id] = !!x.hidden);
  const hook = (d.triggers || []).some(t => (EVENTS[t.event] || {}).hook);
  const again = () => { const all = d.steps.filter(x => !isEnd(x)), shown = all.filter(x => !hid[x.id]).length;
    const el = shell('<span class="ttl">📦 Return Response</span><span class="kc-sub">what “' + esc(d.name) + '” hands back</span>',
      area("Identity", row("Name", '<input type="text" value="Return Response" disabled>', "The flow's answer: one list, the items of every step the run went through, in that order (" + shown + " of " + all.length + " steps)."))
      + area("Settings", row("Each item as", '<input type="text" data-row value="' + esc(a.row) + '"' + (S.v1 ? " disabled" : "") + '>', "{text} = the row's text; e.g. “- {text}” or “{id}: {text}”.")
        + row("Parts of at most", '<input type="number" data-part min="200" value="' + esc(a.part) + '"' + (S.v1 ? " disabled" : "") + '>', hook ? "Characters per part (Claude Code shows at most 10,000 per hook); cut between items." : "Characters per part, cut between items.")
        + row("Steps in it", '<div style="display:flex;flex-direction:column;gap:8px">' + all.map(x => '<span class="kc-tgl' + (hid[x.id] ? "" : " on") + '" data-hid="' + x.id + '"><i></i>' + (x.b === "flow.call" ? "⧉" : blk(x.b).icon) + ' ' + esc(stepName(x)) + '</span>').join("") + '</div>'))
      + area("Note", '<p class="kc-note">Off: the step still runs and later steps can use its items, but they are not handed back.</p>'),
      '<span class="iss">' + (shown ? "" : "⚠ Every step is off: the answer is empty.") + '</span><span style="display:flex;gap:10px"><button class="btn g s" data-x>Cancel</button>' + (S.v1 ? '' : '<button class="btn s" data-ok>Apply</button>') + '</span>');
    if (S.v1) return;
    el.querySelector("[data-row]").oninput = e => { a.row = e.target.value; };
    el.querySelector("[data-part]").onchange = e => { a.part = Math.max(200, parseInt(e.target.value, 10) || 9000); };
    el.querySelectorAll("[data-hid]").forEach(t => t.onclick = () => { hid[t.dataset.hid] = !hid[t.dataset.hid]; again(); });
    el.querySelector("[data-ok]").onclick = () => { d.answer = a; d.steps.forEach(x => { if (isEnd(x)) return; if (hid[x.id]) x.hidden = true; else delete x.hidden; }); closeShell(); render(); toast("Applied. Save the flow to keep it."); }; };
  again();
}

// ── the space picker: the context spaces first (known when the flow runs), then a search over the spaces the caller
// can read, 10 at a time (GET /spaces?q=&limit=10&offset=); Recent 5 before anything is typed
function pickSpace(cur, done, opts){ PK = {q:"", page:0, cur, done, space:true, noCtx:!!(opts && opts.noCtx)}; drawSpacePick(); }
async function drawSpacePick(){
  const o = PK; if (!o) return; let page = {total:0, items:[]};
  if (o.q.trim()) { try { page = await api("GET", "/spaces?limit=10&offset=" + o.page * 10 + "&q=" + encodeURIComponent(o.q.trim())); } catch(e) { page = {total:0, items:[], err:e.message}; } }
  if (PK !== o) return;
  const item = (v, t, sub) => '<div data-psv="' + esc(v) + '" class="' + (o.cur === v ? "on" : "") + '"><b>' + esc(t) + '</b><small>' + esc(sub) + '</small></div>';
  const sp = x => item(x.id, x.name || x.id, (x.desc ? x.desc.slice(0, 100) + " · " : "") + (x.type || "") + " space · " + (x.vis || ""));
  const recent = !o.q.trim() ? recentGet("spaces").slice(0, 5) : [], pages = Math.max(1, Math.ceil(page.total / 10));
  const el = pickHost("space");
  el.innerHTML = '<div class="kc-modal" style="width:min(580px,100%)"><div class="kc-mhd"><span class="ttl">Which space?</span><span class="sp"></span><button class="xb" data-pkx title="Close (Esc)">✕</button></div><div class="kc-mbody">'
    + (o.noCtx ? '' : '<div><div class="kc-hint" style="margin:0 0 4px">From the context: known only when the flow runs</div><div class="kc-pick">' + CTX_SPACES.map(c => item(c.v, c.l, c.d)).join("") + '</div></div>')
    + '<input type="text" class="kc-search" data-psq placeholder="Search the spaces you can read, by name or what they are" value="' + esc(o.q) + '">'
    + (recent.length ? '<div class="kc-hint">Recent</div><div class="kc-pick">' + recent.map(v => item(v, v, "picked before")).join("") + '</div>' : (!o.q.trim() ? '<p class="kc-hint">Type to find a space.</p>' : ''))
    + (o.q.trim() ? '<div class="kc-pick" id="kcPsList">' + page.items.map(sp).join("") + '</div>' + (page.err ? '<div class="kc-warn">' + esc(page.err) + '</div>' : page.total ? '' : '<p class="kc-hint">No space matches.</p>')
      + (page.total > 10 ? '<div class="kc-pager"><button class="btn g s" data-psp="-1"' + (o.page ? "" : " disabled") + '>‹ Previous</button><span class="kc-hint">Page ' + (o.page + 1) + ' of ' + pages + ' · ' + page.total + ' spaces</span><button class="btn g s" data-psp="1"' + (o.page + 1 < pages ? "" : " disabled") + '>Next ›</button></div>' : '') : '')
    + '</div><div class="kc-mft"><span class="iss"></span><span style="display:flex;gap:10px"><button class="btn g s" data-pkx>Cancel</button></span></div></div>';
  el.querySelectorAll("[data-pkx]").forEach(b => b.onclick = closePick);
  const qi = el.querySelector("[data-psq]"); qi.oninput = () => { o.q = qi.value; o.page = 0; clearTimeout(o.t); o.t = setTimeout(() => drawSpacePick().then(() => { const n = document.querySelector("[data-psq]"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } }), 180); };
  if (o.focus !== false) { qi.focus(); qi.setSelectionRange(qi.value.length, qi.value.length); }
  el.querySelectorAll("[data-psp]").forEach(b => b.onclick = () => { o.page += +b.dataset.psp; o.focus = false; drawSpacePick(); });
  el.querySelectorAll("[data-psv]").forEach(dv => dv.onclick = () => { const v = dv.dataset.psv, f = o.done; if (v[0] !== "@") recentAdd("spaces", v, 5); closePick(); f(v); });
}

// ═════════════════════════════ TRY IT, WAITING RUNS, CHECK AS (K27) ═════════════════════════════
// Try it runs the SAVED flow (POST /flows/{id}/run; a dirty draft is saved first) with a prompt (input.text) and a
// payload (the rest of the input). The trace badges every node on the canvas (✓ / ✕ / skipped, ms) and greys the
// branches the run did not take. A run that reaches Ask the Agent waits: the panel shows the ask and takes the answer
// (POST /flows/runs/{run_id}/answer); KH's 422 reason shows and the run stays waiting. 'Check as' (sign-in off only)
// sends X-KH-Check-As: a flow grants nothing, so the run sees what that caller would see. KH keys a run to its caller
// (the check-as frame is a caller of its own), so the frame a run started under is kept per run (localStorage) and
// sent again with its answer; the Waiting runs list asks once per frame and so lists every waiting run of this KH.
const TRY_AS = [["", "Me (my own rights)"], ["owner", "Owner"], ["member", "Member"], ["technical", "Technical account"], ["guest", "Guest (not a member)"]];
const asLabel = v => (TRY_AS.find(x => x[0] === v) || ["", v])[1];
const FRAMES = "kh_home_run_frames";
function frames(){ try { return JSON.parse(localStorage.getItem(FRAMES) || "{}"); } catch(e) { return {}; } }
function frameSet(id, as){ try { const f = frames(); if (as) f[id] = as; else delete f[id]; localStorage.setItem(FRAMES, JSON.stringify(f)); } catch(e) {} }
// api() with a Check as frame (api() reads api.checkAs before its first await)
function asCall(as, m, path, body){ api.checkAs = as || ""; try { return api(m, path, body); } finally { api.checkAs = ""; } }
function tst(){ if (!S.test || S.test.fid !== S.fid) S.test = {fid:S.fid, prompt:"", payload:"", as:"", trace:{}, order:[]}; return S.test; }
function payloadOf(t){ const raw = String(t.payload || "").trim(); if (!raw) return {ok:true, v:{}};
  try { const v = JSON.parse(raw); return v && typeof v === "object" && !Array.isArray(v) ? {ok:true, v} : {ok:false, why:'the payload is a JSON object, like {"space": "kh-investigate"}'}; }
  catch(e) { return {ok:false, why:"not JSON yet (" + e.message + ")"}; } }
function inputOf(t){ const p = payloadOf(t); if (!p.ok) return p; const v = {...p.v}; if (String(t.prompt || "").trim() && !("text" in v)) v.text = t.prompt.trim(); return {ok:true, v}; }
const shapeIsJson = sh => { try { const o = JSON.parse(String(sh || "")); return !!o && typeof o === "object" && !Array.isArray(o); } catch(e) { return false; } };
function ago(ts){ const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000); return s < 60 ? Math.round(s) + " s ago" : s < 3600 ? Math.round(s / 60) + " min ago" : s < 86400 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago"; }
// one run's trace, merged over its answers (each answer's trace repeats or extends the one before)
function took(t, out){ t.out = out; t.run_id = out.status === "waiting" ? out.run_id : null; if (out.run_id) t.last_run = out.run_id;
  (out.trace || []).forEach(r => { if (!(r.id in t.trace)) t.order.push(r.id); t.trace[r.id] = r; }); }

// what the canvas shows of the last run of this flow (null: no run, or the draft changed since)
function runView(){
  const t = S.test; if (!t || t.fid !== S.fid || !t.out || fdirty()) return null;
  const tr = t.trace, out = t.out, d = fd(), wait = out.status === "waiting" ? (d.steps.find(x => x.name === (out.ask || {}).step) || {}).id : null;
  const endStep = t.order.some(id => isEnd(fstep(id)));
  const endKey = wait || endStep ? null : out.status === "failed" ? FAIL : END;
  const tookEdge = (from, kind) => { if (from === "start") return true; const r = tr[from]; if (!r) return false; return (kind === "ok" || kind === "yes") ? r.exit === "ok" : r.exit === "fail"; };
  const st = key => { if (key === END || key === FAIL) return endKey === key ? (key === END ? "ok" : "fail") : "skip";
    if (key === wait) return "wait"; const r = tr[key]; if (!r) return "skip";
    if (r.exit === "end") { const x = fstep(key); return x && x.b === "end.stop" && (x.args.outcome || "failed") === "failed" ? "fail" : "ok"; }
    return r.ok === false ? "fail" : "ok"; };
  // opened from Waiting runs: no trace yet, only the waiting step is marked
  if (!t.order.length) return {took:() => true, cls:key => key === wait ? " run-wait" : "", badge:key => key === wait ? '<span class="kc-rb wait">⏳ waiting for the agent</span>' : ""};
  return {took:tookEdge, cls:key => " run-" + st(key), badge:key => { const s = st(key), r = tr[key];
    const txt = s === "wait" ? "⏳ waiting for the agent" : s === "skip" ? "skipped" : (key === END || key === FAIL || (r && r.exit === "end")) ? "● ended here"
      : (s === "ok" ? "✓ " : "✕ ") + (r.b === "logic.decide" ? (r.exit === "ok" ? "yes · " : "no · ") : "") + (r.ms || 0) + " ms";
    return '<span class="kc-rb ' + s + '" title="' + esc(r && r.error ? r.error : r && r.note ? r.note : txt) + '">' + esc(txt) + '</span>'; }};
}
// 🔒 on the kh.call steps the caller (or the Check as frame) cannot read: GET /spaces/{space}/access per space
async function lockCheck(input){
  const t = tst(), d = fd(); S.locks = {}; t.hidden = ""; if (!d) return;
  const spaceOf = x => { const sp = x.args.space; if (!sp) return ""; if (sp[0] !== "@") return sp; return ["@bound", "@named", "@input"].includes(sp) ? String((input || {}).space || "") : ""; };
  const steps = d.steps.filter(x => x.b === "kh.call" && !TURN_OPS.includes(x.args.op) && spaceOf(x)), acc = {};
  await Promise.all([...new Set(steps.map(spaceOf))].map(sp => asCall(t.as, "GET", "/spaces/" + encodeURIComponent(sp) + "/access").then(r => { acc[sp] = r; }).catch(e => { acc[sp] = e.status === 404 ? null : undefined; })));
  const who = t.as ? "as " + asLabel(t.as).toLowerCase() : "with your rights";
  steps.forEach(x => { const sp = spaceOf(x), a = acc[sp], sc = x.args.scope;
    if (a === null) { S.locks[x.id] = "“" + sp + "” is not visible " + who + ": KH answers as if it did not exist"; if (sp === (input || {}).space) t.hidden = sp; }
    else if (a && sc && a.scopes && a.scopes[sc] && !a.scopes[sc].read) S.locks[x.id] = "cannot read " + sp + "/" + sc + " " + who + " (" + (a.scopes[sc].why || "no read right") + ")"; });
}

function tryHtml(){
  const t = tst(), p = payloadOf(t), dirty = fdirty();
  return '<div class="card kc-test" id="ffTry" style="margin-top:12px"><div class="kc-try-hd"><b>Try it</b><span class="kc-hint">Runs the saved flow with the rights of the caller in Check as: a flow grants nothing.</span></div>'
    + '<div class="row" style="margin-top:8px"><input id="ffPrompt" placeholder="Prompt: goes in as input.text" value="' + esc(t.prompt || "") + '">'
    + '<label class="kc-as">Check as <select id="ffAs" title="Sign-in off only: KH runs the flow as this kind of caller">' + TRY_AS.map(([v, l]) => '<option value="' + v + '"' + (t.as === v ? " selected" : "") + '>' + esc(l) + '</option>').join("") + '</select></label></div>'
    + '<label class="kc-plab" for="ffPayload">Payload <small class="kc-hint">a JSON object, the rest of the input (' + esc(Object.keys((S.meta || {}).input || {}).join(", ") || "space, …") + ')</small></label>'
    + '<textarea id="ffPayload" rows="2" spellcheck="false" placeholder=\'{"space": "kh-investigate"}\'>' + esc(t.payload || "") + '</textarea>'
    + '<div class="kc-hint ' + (p.ok ? "ok" : "no") + '" id="ffPayChk">' + (String(t.payload || "").trim() ? (p.ok ? "✓ JSON object" : "✕ " + esc(p.why)) : "") + '</div>'
    + '<div class="row"><button class="btn g s" id="ffMatch">Which flow would KH serve?</button><button class="btn s" id="ffRun"' + (t.busy ? " disabled" : "") + '>' + (dirty ? "Save and run" : "Run this flow") + '</button>'
    + (t.out || t.err ? '<button class="btn g s" id="ffClear">Clear the run</button>' : '') + '</div>'
    + '<div id="ffRes">' + testHtml() + '</div></div>';
}
function testHtml(){
  const t = tst(), out = [];
  if (t.match) { const m = t.match;
    out.push('<p class="kc-note" id="ffMatchRes">' + (m.matched ? 'KH would serve <b>' + esc((S.fcache[m.matched] || {}).name || m.matched) + '</b> <span class="kc-mono">' + esc(m.matched) + '</span> · score ' + esc(m.score)
      + (m.matched === S.fid ? ' · this flow' : ' <button class="btn g s" data-mopen="' + esc(m.matched) + '">Open it</button>') : 'No task flow is close to this prompt: KH serves none.') + '</p>'); }
  if (t.busy) out.push('<p class="kc-hint" style="margin-top:8px">Running…</p>');
  if (t.err) out.push('<div class="kc-warn" style="margin-top:8px" id="ffErr">' + esc(t.err) + '</div>');
  const o = t.out; if (!o) return out.join("");
  const st = o.status || "done", tag = st === "waiting" ? "wait" : st === "failed" ? "fail" : st === "done" ? "ok" : "q";
  out.push('<p style="margin:10px 0 4px" id="ffStatus"><span class="kc-tag st-' + tag + '">' + esc(st) + '</span> ' + esc(o.message && o.message !== o.text ? o.message : "")
    + (t.as ? ' <span class="kc-hint">as ' + esc(asLabel(t.as).toLowerCase()) + '</span>' : '') + (t.last_run ? ' <small class="kc-mono kc-hint">' + esc(t.last_run) + '</small>' : '') + '</p>');
  if (t.hidden) out.push('<div class="kc-warn" id="ffNone">Nothing in what you can see' + (t.as ? " as " + esc(asLabel(t.as).toLowerCase()) : "") + ': KH answers as if “' + esc(t.hidden) + '” did not exist. The 🔒 steps cannot read it.</div>');
  if (st === "waiting" && o.ask) { const a = o.ask, js = shapeIsJson(a.shape);
    out.push('<div class="kc-ask" id="ffAsk"><h4>⏳ Waiting at “' + esc(a.step || "") + '”: the agent’s answer</h4>' + row("Task", '<div class="kc-note">' + esc(a.task || "") + '</div>')
      + (a.guide ? row("Guide", '<div class="kc-note">' + esc(a.guide) + '</div>') : '') + row("Answer must look like", '<code class="kc-mono">' + esc(a.shape || "a text") + '</code>')
      + '<textarea id="ffAnswer" rows="5" spellcheck="false" placeholder="' + (js ? "A JSON object of that shape" : "The answer text") + '">' + esc(t.answer || "") + '</textarea>'
      + '<div class="kc-hint" id="ffAnsChk">' + ansChk(t.answer, js) + '</div>'
      + (t.aerr ? '<div class="kc-warn" id="ffAerr">KH refused the answer: ' + esc(t.aerr) + '<br><small>The run still waits; fix the answer and send it again.</small></div>' : '')
      + '<div class="row"><button class="btn s" id="ffSend"' + (t.sending ? " disabled" : "") + '>' + (t.sending ? "Sending…" : "Send answer") + '</button></div></div>'); }
  const tr = t.order.map(id => t.trace[id]).map(r => '<div class="kc-trace"><span class="' + (r.ok === false ? "no" : "ok") + '">' + (r.ok === false ? "✕" : "✓") + '</span><b>' + esc(r.name || r.id) + '</b><span class="kc-hint">' + esc(r.b || r.node || "") + (r.b === "logic.decide" && r.exit ? ' · ' + (r.exit === "ok" ? "yes" : "no") + ' exit' : '') + '</span><span class="kc-hint" style="margin-left:auto">' + (r.ok === false ? esc(r.error || "failed") : (r.ms || 0) + " ms") + '</span></div>').join("");
  if (tr) out.push('<div class="kc-tracebox">' + tr + '</div>');
  if (st !== "waiting") out.push('<div class="kc-pack" id="ffText">' + esc(o.text || (o.output || {}).text || o.message || "") + '</div>');
  return out.join("");
}
function ansChk(v, js){ if (!String(v || "").trim()) return ""; if (!js) return '<span class="ok">✓ a text</span>';
  try { const o = JSON.parse(v); return o && typeof o === "object" && !Array.isArray(o) ? '<span class="ok">✓ JSON object</span>' : '<span class="no">✕ not a JSON object: KH will refuse it</span>'; }
  catch(e) { return '<span class="no">✕ not JSON: KH will refuse it (' + esc(e.message) + ')</span>'; } }
function redrawTry(){ const el = $("ffTry"); if (!el) return; el.outerHTML = tryHtml(); wireTry(); }
function wireTry(){
  const t = tst(), P = $("ffPrompt"); if (!P) return;
  P.oninput = e => { t.prompt = e.target.value; }; P.onkeydown = e => { if (e.key === "Enter") runTest(); };
  $("ffPayload").oninput = e => { t.payload = e.target.value; const p = payloadOf(t), c = $("ffPayChk"); c.className = "kc-hint " + (p.ok ? "ok" : "no"); c.textContent = t.payload.trim() ? (p.ok ? "✓ JSON object" : "✕ " + p.why) : ""; };
  $("ffAs").onchange = async e => { t.as = e.target.value; const i = inputOf(t); await lockCheck(i.ok ? i.v : {}); drawCanvas(); redrawTry(); };
  $("ffMatch").onclick = matchTest; $("ffRun").onclick = runTest;
  const cl = $("ffClear"); if (cl) cl.onclick = () => { Object.assign(t, {out:null, err:"", trace:{}, order:[], run_id:null, last_run:null, aerr:"", answer:"", match:null, hidden:""}); S.locks = {}; drawCanvas(); redrawTry(); };
  const A = $("ffAnswer"); if (A) { const js = shapeIsJson(((t.out || {}).ask || {}).shape); A.oninput = () => { t.answer = A.value; $("ffAnsChk").innerHTML = ansChk(A.value, js); }; }
  const sd = $("ffSend"); if (sd) sd.onclick = sendAnswer;
  document.querySelectorAll("[data-mopen]").forEach(b => b.onclick = () => openFlow(b.dataset.mopen));
}
async function matchTest(){
  const t = tst(), text = String(t.prompt || "").trim(); if (!text) return toast("Type a prompt first: KH matches the prompt to a task flow.");
  const i = inputOf(t); t.err = "";
  try { t.match = await asCall(t.as, "POST", "/flows/match", {text, project:(i.ok && i.v.space) || undefined});
    const m = t.match.matched;
    if (m && !(S.fcache[m] || {}).name) await api("GET", "/flows/" + encodeURIComponent(m)).then(f => { S.fcache[m] = {...(S.fcache[m] || {}), id:m, name:f.name}; }).catch(() => {}); }
  catch(e) { t.match = null; t.err = e.message; }
  redrawTry();
}
async function runTest(){
  const t = tst(), i = inputOf(t);
  if (!i.ok) { t.err = "Payload: " + i.why; return redrawTry(); }
  if (!Object.keys(i.v).length) return toast("Type a prompt or a payload to try.");
  if (fdirty()) { if (S.parentsWant) return toast("Save first: this save also changes other flows.", 6000); if (!await saveFlowBody()) return; }
  Object.assign(t, {out:null, err:"", trace:{}, order:[], run_id:null, last_run:null, aerr:"", answer:"", busy:true, hidden:""}); redrawTry();
  try { const out = await asCall(t.as, "POST", "/flows/" + encodeURIComponent(S.fid) + "/run", {input:i.v}); took(t, out); if (out.status === "waiting") frameSet(out.run_id, t.as); }
  catch(e) { t.err = e.message; }
  t.busy = false; await lockCheck(i.v); await refreshWaiting(); render(); tryInView();
}
async function sendAnswer(){
  const t = tst(), o = t.out; if (!o || !t.run_id) return;
  const raw = String(t.answer || ""); if (!raw.trim()) return toast("Write the answer first.");
  let ans = raw; if (shapeIsJson(o.ask.shape)) { try { ans = JSON.parse(raw); } catch(e) { ans = raw; } }   // not JSON: KH says why it refuses
  const id = t.run_id, as = id in frames() ? frames()[id] : t.as;
  t.aerr = ""; t.sending = true; redrawTry();
  try { const out = await asCall(as, "POST", "/flows/runs/" + encodeURIComponent(id) + "/answer", {answer:ans}); took(t, out); t.answer = ""; if (out.status !== "waiting") frameSet(id, null); }
  catch(e) { const d = e.data && e.data.detail; t.aerr = (d && typeof d === "object" ? d.error : d) || e.message; }
  t.sending = false; await refreshWaiting(); render(); tryInView();
}
function tryInView(){ const el = $("ffTry"); if (el) el.scrollIntoView({block:"nearest"}); }

// Waiting runs: GET /flows/runs?status=waiting once per Check as frame (each frame is its own caller), newest first
S.waiting = [];
async function refreshWaiting(){
  // Check as frames work only with sign-in off; a signed-in KH ignores the header, so one call is enough there.
  const frames = (typeof cfg !== "undefined" && cfg.auth) ? [[""]] : TRY_AS;
  const got = await Promise.all(frames.map(([as]) => asCall(as, "GET", "/flows/runs?status=waiting&limit=50").then(r => (r.runs || []).map(x => ({...x, as}))).catch(() => [])));
  const seen = new Set(); S.waiting = got.flat().filter(x => !seen.has(x.run_id) && seen.add(x.run_id)).sort((a, b) => String(b.created).localeCompare(String(a.created)));
  await Promise.all([...new Set(S.waiting.map(x => x.flow).filter(f => !(S.fcache[f] || {}).name))].map(f => api("GET", "/flows/" + encodeURIComponent(f)).then(r => { S.fcache[f] = {...(S.fcache[f] || {}), id:f, name:r.name}; }).catch(() => {})));
  navBadge();
}
function navBadge(){ const a = document.querySelector('a[data-v="flows"] .nl b'); if (!a) return; let b = document.getElementById("ffNavBadge");
  if (!S.waiting.length) { if (b) b.remove(); return; }
  if (!b) { b = document.createElement("span"); b.id = "ffNavBadge"; a.appendChild(b); }
  b.textContent = S.waiting.length; b.title = S.waiting.length + " flow run" + (S.waiting.length === 1 ? "" : "s") + " wait for an answer"; }
function waitHtml(){
  const w = S.waiting || [];
  return '<div class="card kc-wait" id="ffWait"><div class="kc-try-hd"><b>Waiting runs</b> <span class="kc-tag' + (w.length ? " st-wait" : "") + '" id="ffWaitN">' + w.length + '</span>'
    + '<span class="kc-hint">' + (w.length ? "Runs stopped at Ask the Agent, waiting for an answer. Sign-in is off here: the browser and the agents are the same local caller, so every waiting run of this KH is listed." : "No run waits for an answer.") + '</span></div>'
    + w.map(x => '<div class="kc-wrow" data-wrun="' + esc(x.run_id) + '"><b>' + esc((S.fcache[x.flow] || {}).name || x.flow) + '</b><span>at “' + esc((x.ask || {}).step || "?") + '”</span>'
      + '<span class="kc-hint">' + esc(ago(x.created)) + '</span><span class="kc-hint">started by ' + (x.as ? "Check as " + esc(asLabel(x.as).toLowerCase()) : "the local caller") + '</span>'
      + '<small class="kc-mono kc-hint">' + esc(x.run_id) + '</small><button class="btn g s" data-wopen="' + esc(x.run_id) + '">Open</button></div>').join("") + '</div>';
}
function wireWait(){ document.querySelectorAll("[data-wopen]").forEach(b => b.onclick = () => openWaiting(S.waiting.find(x => x.run_id === b.dataset.wopen))); }
async function openWaiting(run){
  if (!run) return;
  if (fdirty() && S.fid !== run.flow && !confirm("Leave “" + fd().name + "” without saving?")) return;
  if (S.fid !== run.flow) await openFlow(run.flow);
  S.test = {fid:run.flow, prompt:"", payload:"", as:run.as || "", trace:{}, order:[], out:{status:"waiting", run_id:run.run_id, ask:run.ask, message:""}, run_id:run.run_id, last_run:run.run_id};
  frameSet(run.run_id, run.as || ""); S.locks = {}; render(); tryInView();
  const a = $("ffAnswer"); if (a) a.focus();
}
async function saveFlowBody(extra){
  const d = fd(); if (!d || S.v1) return;
  const body = {kind:S.kind || "task", enabled:d.enabled !== false, ...saveBody(d)}; if (body.gridMoved) { delete body.grid; delete body.gridMoved; }
  try { const r = await api("PUT", "/flows/" + encodeURIComponent(S.fid), body);
    S.warn = r.warnings || []; S.sprob = r.problems || []; S.fmsg = ""; toast("Saved “" + r.name + "” (v" + r.version + ")." + (S.warn.concat(S.sprob).length ? " KH warns: " + S.warn.concat(S.sprob)[0] : " Live within seconds, no redeploy."), 6000);
    S.meta = r; S.kind = r.kind; S.draft = normV2(r); S.base = JSON.stringify(saveBody(S.draft)); delete S.isNew;
    if (extra) await extra(); await flowsPage(true); await parentsOf(S.fid); render(); return true; }
  catch(e){ S.fmsg = e.message; render(); toast(e.message, 9000); return false; }
}
async function delFlow(){
  try { const r = await api("DELETE", "/flows/" + encodeURIComponent(S.fid)); toast("Flow " + r.result + "."); const id = S.fid;
    await flowsPage(true); if (r.result === "deleted") { S.fid = null; S.draft = null; S.meta = null; render(); } else openFlow(id); }
  catch(e){ S.fmsg = e.message; render(); toast(e.message, 8000); }
}
document.addEventListener("keydown", e => {
  if (e.key !== "Delete" || S.tab !== "flow" || !fd() || S.v1 || $("kcForm") || $("kcPick") || /INPUT|SELECT|TEXTAREA/.test((document.activeElement || {}).tagName)) return;
  const v = $("v-flows"); if (!v || v.classList.contains("hide")) return;
  if (S.sel && S.sel[0] !== "@" && S.sel !== "start") removeStep(S.sel);
});

// ═════════════════════════════ START NODE, NEW FLOW, PARENTS (K24b) ═════════════════════════════
// What starts a flow: 0..n triggers (an event: a Claude Code hook, the agent calling it by name, a KH event; or a
// prompt close to example prompts, maybe only in one space) plus 0..n flows that run it as a step. All of it can
// change at any time in its ⚡ Start node.
const TRIG_TYPES = [["event", "⚡ On an event", "A Claude Code hook (SessionStart, PreToolUse…), an agent calling the flow by name, or a KH event."],
  ["prompt", "💬 When a prompt asks for it", "Runs when a prompt is close in meaning to one of the example prompts, in any language."]];
const notFired = e => e && e.fired === false;
// which flow already has each hook event (one flow per hook event): GET /flows?trigger=event, one page of 100
async function hookOwners(){ try { const r = await api("GET", "/flows?trigger=event&limit=100"); const out = {};
    r.items.forEach(f => { S.fcache[f.id] = {...(S.fcache[f.id] || {}), ...f}; (f.triggers || []).forEach(t => { if (t.type === "event" && (EVENTS[t.event] || {}).hook && f.enabled !== false) out[t.event] = f; }); }); return out; }
  catch(e) { return {}; } }
function triggersHtml(t, owners){
  const one = (x, i) => {
    if (x.type === "event") { const e = EVENTS[x.event];
      return '<div class="kc-trg"><button class="xt" data-tdel="' + i + '" title="Remove this trigger">✕</button>'
        + row("Event", '<select data-ev="' + i + '"><option value="">(pick one)</option>' + EV_SOURCES.map(g => '<optgroup label="' + esc(g.source) + '">' + g.events.map(ev => { const o = ev.hook && owners[ev.id] && owners[ev.id].id !== t.self ? owners[ev.id] : null;
            return '<option value="' + ev.id + '"' + (x.event === ev.id ? " selected" : "") + (o ? " disabled" : "") + '>' + esc(ev.label) + ' · ' + esc(ev.desc) + (o ? " (already: " + esc(o.name) + ")" : notFired(ev) ? " · KH does not fire this yet" : "") + '</option>'; }).join("") + '</optgroup>').join("") + '</select>',
          !e ? "Events come from Claude Code hooks, from agents calling the flow, or from KH itself."
            : notFired(e) ? '<span class="kc-nf">KH does not fire this yet: the trigger is kept, but it does not start the flow for now.</span>'
            : e.id === "kh.daily_0200" ? 'Off by default: KH fires it by itself only when KH_DAILY_AT is set. <button class="btn g s" data-runnow>Run now</button> runs this saved flow now as the daily event would. <b data-runmsg></b>'
            : e.id === "kh.agent_call" ? "kh-helper offers this flow as the MCP tool " + esc(toolNameOf(t.self)) + "; an agent calls it by that name with its own input."
            : e.hook ? "A Claude Code hook: kh-helper's hook asks KH, KH runs this flow. One flow per hook event." : esc(e.desc)) + '</div>'; }
    return '<div class="kc-trg"><button class="xt" data-tdel="' + i + '" title="Remove this trigger">✕</button>'
      + row("Prompts like", '<div class="kc-chipsbox">' + (x.examples || []).map((p, j) => '<span class="kc-expill">' + esc(p) + '<button data-exdel="' + i + ':' + j + '" title="Remove">✕</button></span>').join("") + '<input type="text" data-ex="' + i + '" placeholder="type a prompt, press Enter"></div>', "Three to five ways people really ask for it, in any language.")
      + row("Only in space", pickField("tsp=\"" + i + "\"", x.space || "", "Any space") + (x.space ? '<button class="btn g s" data-tspx="' + i + '" style="margin-top:6px">Any space instead</button>' : ''), "Any space: whatever space the session is on.") + '</div>';
  };
  return area("When does it run?", (t.triggers.length ? t.triggers.map(one).join("") : '<p class="kc-hint" style="margin:0 0 8px">No trigger of its own.</p>')
    + '<button class="btn g s" data-tadd style="align-self:flex-start;margin-bottom:12px">+ Add trigger</button>'
    + row("Run as a step by", pickField("pman", t.parents.length ? t.parents.length + " flow" + (t.parents.length > 1 ? "s" : "") + ": " + t.parents.map(p => (S.fcache[p] || {}).name || p).slice(0, 3).join(" · ") + (t.parents.length > 3 ? " and " + (t.parents.length - 3) + " more" : "") : "", "No flow runs it yet: pick flows…"),
      "Tick the flows that run it. Added: a ⧉ step goes at the end of that flow; removed: that step leaves it. Save asks before it changes them."));
}
const toolNameOf = id => "kh_" + (String(id || "").replace(/^flow\./, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "flow");
function trigIssue(t){
  if (t.triggers.some(x => x.type === "event" && !x.event)) return "Pick the event of each event trigger.";
  if (t.triggers.some(x => x.type === "prompt" && !(x.examples || []).length)) return "A prompt trigger needs at least one example prompt.";
  const hooks = t.triggers.filter(x => x.type === "event" && (EVENTS[x.event] || {}).hook).map(x => x.event);
  if (hooks.length !== new Set(hooks).size) return "A hook event is listed twice.";
  return "";
}
function wireTrigger(el, t, again){
  el.querySelectorAll("[data-tdel]").forEach(b => b.onclick = () => { t.triggers.splice(+b.dataset.tdel, 1); again(); });
  el.querySelectorAll("[data-ev]").forEach(s => s.onchange = () => { t.triggers[+s.dataset.ev].event = s.value; again(); });
  el.querySelectorAll("[data-tsp]").forEach(s => s.onclick = () => { const x = t.triggers[+s.dataset.tsp]; pickSpace(x.space || "", v => { x.space = v; again(); }, {noCtx:true}); });
  el.querySelectorAll("[data-tspx]").forEach(s => s.onclick = () => { t.triggers[+s.dataset.tspx].space = ""; again(); });
  el.querySelectorAll("[data-ex]").forEach(ex => ex.onkeydown = e => { if (e.key === "Enter" && ex.value.trim()) { e.preventDefault(); const i = ex.dataset.ex; (t.triggers[+i].examples = t.triggers[+i].examples || []).push(ex.value.trim()); again();
    setTimeout(() => { const n = document.querySelector('[data-ex="' + i + '"]'); if (n) n.focus(); }, 0); } });
  el.querySelectorAll("[data-exdel]").forEach(b => b.onclick = () => { const [i, j] = b.dataset.exdel.split(":").map(Number); t.triggers[i].examples.splice(j, 1); again(); });
  el.querySelector("[data-tadd]").onclick = e => { const r = e.target.getBoundingClientRect();
    menu(r.left, r.bottom + 6, [{head:"Add a trigger"}, ...TRIG_TYPES.map(([k, l, dd]) => ({label:l, sub:dd, k}))], it => { t.triggers.push(it.k === "event" ? {type:"event", event:""} : {type:"prompt", examples:[], space:""}); again(); }, true); };
  el.querySelector("[data-pman]").onclick = () => pickFlow({title:"Which flows run it as a step?", multi:true, chosen:t.parents, skip:id => id === t.self,
    hint:"Tick the flows that run it. A flow cannot run itself; KH refuses a loop of flows when you save.", onDone:v => { t.parents = v; again(); }});
}
// The Start node: Identity (name, what it is for, on/off), then the triggers and the parents, then notes.
async function openStart(){
  const d = fd(), owners = await hookOwners();
  const t = {name:d.name, desc:d.desc || "", enabled:d.enabled !== false, triggers:clone(d.triggers || []), parents:(S.parentsWant || S.parents || []).slice(), self:S.fid};
  const again = () => { const iss = trigIssue(t), never = !t.triggers.length && !t.parents.length;
    const el = shell('<span class="ttl">⚡ Start</span><span class="kc-sub">when “' + esc(d.name) + '” runs</span>',
      area("Identity", row("Name", '<input type="text" data-fnm value="' + esc(t.name) + '"' + (S.v1 ? " disabled" : "") + '>') + row("What it is for", '<input type="text" data-ds value="' + esc(t.desc) + '"' + (S.v1 ? " disabled" : "") + '>', "One line; agents and the tool list show it.")
        + row("Enabled", '<span class="kc-tgl' + (t.enabled ? " on" : "") + '" data-en><i></i>' + (t.enabled ? "On: KH runs it" : "Off: KH skips it") + '</span>'))
      + triggersHtml(t, owners)
      + area("Note", '<p class="kc-note">Every way in starts at the same first step (pull a dot of the Start node to change it). A hook event belongs to one flow at a time. Events marked “KH does not fire this yet” are kept but start nothing for now; KH events run the flow in the background with the rights of whoever caused them.</p>'),
      '<span class="iss">' + (iss ? "⚠ " + esc(iss) : never ? "⚠ No trigger and no flow runs it: it never runs." : "") + '</span><span style="display:flex;gap:10px"><button class="btn g s" data-x>Cancel</button>' + (S.v1 ? '' : '<button class="btn s" data-ok' + (iss ? " disabled" : "") + '>Apply</button>') + '</span>');
    el.querySelector(".kc-modal").style.width = "min(820px,100%)";
    if (S.v1) return;
    el.querySelector("[data-fnm]").oninput = e => { t.name = e.target.value; };
    el.querySelector("[data-ds]").oninput = e => { t.desc = e.target.value; };
    el.querySelector("[data-en]").onclick = () => { t.enabled = !t.enabled; again(); };
    wireTrigger(el, t, again);
    const rn = el.querySelector("[data-runnow]"); if (rn) rn.onclick = async () => { const m = el.querySelector("[data-runmsg]");
      try { const r = await api("POST", "/flows/events/kh.daily_0200/fire?flow=" + encodeURIComponent(S.fid)), txt = r.queued ? "Started: see its run in GET /flows/runs?origin=event." : "Not started (rate or loop guard).";
        if (m) m.textContent = txt; toast("Daily event: " + txt); } catch(err){ if (m) m.textContent = err.message; toast(err.message, 6000); } };
    el.querySelector("[data-ok]").onclick = () => { Object.assign(d, {name:t.name.trim() || d.name, desc:t.desc, enabled:t.enabled, triggers:clone(t.triggers)});
      S.parentsWant = sameSet(t.parents, S.parents || []) ? null : t.parents.slice(); closeShell(); render(); toast("Applied. Save the flow to keep it" + (S.parentsWant ? " (Save asks before it changes the other flows)." : ".")); }; };
  again();
}
const sameSet = (a, b) => a.length === b.length && a.every(x => b.includes(x));
// make the flows that run `id` as a step match `want`: a ⧉ step at the end of a new parent, taken out of a dropped one
async function applyParents(id, want){
  const have = S.parents || [], add = want.filter(p => !have.includes(p)), drop = have.filter(p => !want.includes(p)), out = [];
  for (const p of add.concat(drop)) {
    const f = await api("GET", "/flows/" + encodeURIComponent(p)), body = clone(f), kind = f.kind; ["id","version","owner","seeded","edited","updated_by","updated_at","warnings","kind"].forEach(k => delete body[k]);
    if (isV2(f)) { const st = body.steps;
      if (add.includes(p)) { const used = new Set(st.map(x => x.id)), names = new Set(st.map(x => x.name)); let k = st.length + 1; while (used.has("s" + k)) k++;
        let nm = fd().name, j = 2; while (names.has(nm)) nm = fd().name + " " + j++;
        const n = {id:"s" + k, b:"flow.call", name:nm, args:{flow:id}, next:{ok:END, fail:FAIL}}, last = [...st].reverse().find(y => !isEnd(y) && (y.next || {}).ok === END);
        if (last) last.next.ok = n.id; st.push(n); }
      else st.filter(x => x.b === "flow.call" && x.args.flow === id).map(x => x.id).forEach(sid => { const x = st.find(y => y.id === sid), to = (x.next || {}).ok || END;
        st.forEach(y => { if (!y.next) return; if (y.next.ok === sid) y.next.ok = to; if (y.next.fail === sid) y.next.fail = FAIL; }); if (body.first === sid) body.first = to[0] === "@" ? undefined : to; st.splice(st.indexOf(x), 1); });
    } else { if (add.includes(p)) { const used = new Set(body.steps.map(x => x.id)); let k = 1; while (used.has("run" + k)) k++; body.steps.push({id:"run" + k, node:"flow.call", title:fd().name.toUpperCase(), args:{flow:id}}); }
      else body.steps = body.steps.filter(x => !(x.node === "flow.call" && (x.args || {}).flow === id)); }
    try { await api("PUT", "/flows/" + encodeURIComponent(p), {kind, enabled:f.enabled !== false, ...body}); out.push((add.includes(p) ? "added to " : "removed from ") + f.name); }
    catch(e) { out.push("✕ " + f.name + ": " + e.message); }
  }
  return out;
}
// Save: the flow itself, then (after a confirm that names them) the flows that run it as a step.
async function saveFlow(){
  const want = S.parentsWant;
  if (!want) return S.isNew ? saveNew() : saveFlowBody();
  const have = S.parents || [], add = want.filter(p => !have.includes(p)), drop = have.filter(p => !want.includes(p));
  const nm = p => esc((S.fcache[p] || {}).name || p) + ' <span class="kc-mono kc-hint">' + esc(p) + '</span>';
  const el = shell('<span class="ttl">Save also changes other flows</span>',
    area("These flows change", (add.length ? '<p class="kc-note">A ⧉ step that runs “' + esc(fd().name) + '” goes at the end of:</p><ul class="kc-ul">' + add.map(p => '<li>' + nm(p) + '</li>').join("") + '</ul>' : '')
      + (drop.length ? '<p class="kc-note">The ⧉ step that runs it leaves:</p><ul class="kc-ul">' + drop.map(p => '<li>' + nm(p) + '</li>').join("") + '</ul>' : ''))
    + area("Note", '<p class="kc-note">Each of them is saved as a new version right after this flow. Wire the new ⧉ step there if it should not simply run last.</p>'),
    '<span class="iss"></span><span style="display:flex;gap:10px"><button class="btn g s" data-x>Cancel</button><button class="btn s" data-ok id="kcSaveAll">Save ' + (1 + add.length + drop.length) + ' flows</button></span>');
  el.querySelector("[data-ok]").onclick = async () => { closeShell();
    const ok = S.isNew ? await saveNew() : await saveFlowBody(); if (!ok) return;
    const res = await applyParents(S.fid, want); S.parentsWant = null; await flowsPage(true); await parentsOf(S.fid); render();
    toast("Saved. Other flows: " + res.join("; "), 8000); };
}
// a new flow is created by its first save (PUT /flows/flow.<slug>)
async function saveNew(){ const ok = await saveFlowBody(); if (ok) { delete S.isNew; recentAdd("flows", S.fid, 8); } return ok; }

let NF = null;
function openNew(){ if (fdirty() && !confirm("Leave “" + fd().name + "” without saving?")) return; NF = {name:"", desc:"", triggers:[], parents:[], from:"", exists:false}; drawNew(); }
// taken: a flow of that name, or of the id the name gives (two list searches; no 404 probing)
async function nameTaken(name){ const id = "flow." + slug(name), nm = name.trim().toLowerCase();
  try { const [a, b] = await Promise.all([api("GET", "/flows?limit=100&q=" + encodeURIComponent(name.trim())), api("GET", "/flows?limit=100&q=" + encodeURIComponent(slug(name)))]);
    return a.items.concat(b.items).some(f => f.id === id || f.name.trim().toLowerCase() === nm); } catch(e) { return false; } }
async function drawNew(){
  const t = NF, id = "flow." + (slug(t.name) || "…"), owners = await hookOwners(); t.self = id;
  const missing = !t.name.trim() ? "Give the flow a name." : t.exists ? "A flow with this name exists." : trigIssue(t);
  const el = shell('<span class="ttl">＋ New flow</span><span class="kc-sub kc-mono">' + esc(id) + '</span>',
    area("Identity", row("Name", '<input type="text" data-nm placeholder="e.g. Deploy Ticketing to QAS" value="' + esc(t.name) + '">' + (t.exists ? '<div class="kc-nf" id="nfExists">A flow with this name exists.</div>' : ''))
      + row("What it is for", '<input type="text" data-ds placeholder="one line; agents and the tool list show it" value="' + esc(t.desc) + '">'))
    + triggersHtml(t, owners)
    + area("Steps", row("Start from", pickField("fromp", t.from ? "A copy of " + ((S.fcache[t.from] || {}).name || t.from) : "", "Empty: I add the steps") + (t.from ? '<button class="btn g s" data-fromx style="margin-top:6px">Start empty instead</button>' : ''), "A copy is yours to change; the original stays as it is."))
    + area("Note", '<p class="kc-note">A flow is the steps KH runs for an agent, each with the caller\'s own rights. Its id is ' + esc(id) + '. Everything here can change later in its ⚡ Start node.</p>'),
    '<span class="iss">' + (missing ? "⚠ " + esc(missing) : !t.triggers.length && !t.parents.length ? "⚠ No trigger and no flow runs it: it never runs until you add one." : "") + '</span><span style="display:flex;gap:10px"><button class="btn g s" data-x>Cancel</button><button class="btn s" data-create' + (missing ? " disabled" : "") + '>Create flow</button></span>');
  el.querySelector(".kc-modal").style.width = "min(820px,100%)";
  const nm = el.querySelector("[data-nm]"); nm.oninput = () => { t.name = nm.value; clearTimeout(t.tm); t.tm = setTimeout(async () => { const ex = t.name.trim() ? await nameTaken(t.name) : false; if (NF !== t) return;
    if (ex !== t.exists || true) { t.exists = ex; await drawNew(); const n = document.querySelector("#kcForm [data-nm]"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } } }, 350); };
  el.querySelector("[data-ds]").oninput = e => { t.desc = e.target.value; };
  const fx = el.querySelector("[data-fromx]"); if (fx) fx.onclick = () => { t.from = ""; drawNew(); };
  el.querySelector("[data-fromp]").onclick = () => pickFlow({title:"Copy the steps of which flow?", chosen:t.from, onDone:fid => { t.from = fid; drawNew(); }});
  wireTrigger(el, t, drawNew);
  el.querySelector("[data-create]").onclick = async () => {
    let steps = [], first, grid, answer;
    if (t.from) { try { const src = await api("GET", "/flows/" + encodeURIComponent(t.from)); const v = isV2(src) ? normV2(src) : fromV1(src); steps = v.steps; first = v.first; grid = v.grid; answer = v.answer; } catch(e) { return toast(e.message, 8000); } }
    S.fid = id; S.isNew = true; S.meta = null; S.kind = "task"; S.v1 = false; S.sel = null; S.from = null; S.fmsg = ""; S.warn = []; S.parents = []; S.parentsWant = t.parents.length ? t.parents.slice() : null;
    S.draft = normV2({name:t.name.trim(), desc:t.desc, enabled:true, triggers:clone(t.triggers), steps, first, grid, answer}); S.base = "";
    S.fcache[id] = {id, name:t.name.trim(), triggers:t.triggers}; NF = null; closeShell(); render();
    toast("Flow “" + t.name.trim() + "” made, not saved yet. " + (steps.length ? "Change its steps, then Save." : "Add its first step with + Add Step, then Save."), 6000); };
  setTimeout(() => { if (!t.name) nm.focus(); }, 0);
}

// ═════════════════════════════ SPACE DATA (K27b, read-only) ═════════════════════════════
// The rows of a space the caller can read, scope by scope, the system scopes included (links, index, cases, events),
// plus three quick filters: Cases (case memory), Open gaps (gap events not closed yet), Learned links (the learned
// links of the rows those gaps and cases name). Nothing here writes. 'Check as' (sign-in off only) sends
// X-KH-Check-As, so a maintainer sees what an owner, member, technical account or guest would see.
const SD = {space:"", scope:"", as:"", filter:"rows", limit:50, rows:[], total:0, truncated:false, next:null, q:"", sel:null, det:null, scopes:[], err:"", busy:false};
const SD_FILTERS = [["rows", "Rows of the scope", "the rows of the scope picked, newest first"], ["cases", "Cases", "solved chains: the rows a task used and its steps"],
  ["gaps", "Open gaps", "rows the agent needed that the map did not show; open until the distiller closes them"],
  ["learned", "Learned links", "links KH learned from closed gaps: from the map's best row to the row really used"]];
const SD_AS = [["", "Me (my own rights)"], ["owner", "Owner"], ["member", "Member"], ["technical", "Technical account"], ["guest", "Guest (not a member)"]];
const sdRoot = () => $("sdRoot");
// every read of this view goes out with the Check as header (api() reads api.checkAs before its first await)
function sdGet(path){ api.checkAs = SD.as || ""; try { return api("GET", path); } finally { api.checkAs = ""; } }
const sdNone = () => "Nothing in what you can see" + (SD.as ? " as " + SD.as : "") + ": KH answers as if " + (SD.space ? "“" + SD.space + "”" : "it") + " did not exist.";
window.loadSpaceData = async function(){ if (!SD.space) { const r = recentGet("spaces"); SD.space = r[0] || ""; } if (SD.space && !SD.scopes.length) await sdScopes(); await sdLoad(); };
async function sdScopes(){ SD.scopes = []; SD.err = "";
  try { const r = await sdGet("/spaces/" + encodeURIComponent(SD.space) + "/scopes"); SD.scopes = r.scopes;
    if (!SD.scope || !SD.scopes.some(x => x.scope === SD.scope)) SD.scope = (SD.scopes.find(x => !x.system && x.rows) || SD.scopes[0] || {}).scope || ""; }
  catch(e) { SD.err = e.status === 404 ? sdNone() : e.message; } }
// Rows and cases page through the rows route (K28c-3): it answers total, has_more and next_offset, so 'Show more'
// shows whenever a next page exists (not only when the scan cap was hit) and appends that page (sdPage).
function sdRowsUrl(offset){ const sc = SD.filter === "cases" ? "cases" : SD.scope;
  return "/spaces/" + encodeURIComponent(SD.space) + "/scopes/" + encodeURIComponent(sc) + "/rows?order=-ts&limit=50&offset=" + offset; }
function sdTakePage(r){ SD.total = r.total != null ? r.total : SD.rows.length; SD.truncated = !!(r.has_more || r.truncated);
  SD.next = r.next_offset != null ? r.next_offset : null; SD.capped = !!r.total_capped; }
async function sdPage(){ if (SD.next == null) return; SD.busy = true; sdRender();
  try { const r = await sdGet(sdRowsUrl(SD.next)); SD.rows = SD.rows.concat(r.items); sdTakePage(r); }
  catch(e) { SD.err = e.status === 404 ? sdNone() : e.message; }
  SD.busy = false; sdRender(); }
async function sdLoad(){
  SD.rows = []; SD.total = 0; SD.truncated = false; SD.next = null; SD.det = null; SD.sel = null;
  if (!SD.space || SD.err) return sdRender();
  SD.busy = true; sdRender();
  const sp = encodeURIComponent(SD.space);
  try {
    if (SD.filter === "rows" || SD.filter === "cases") { const r = await sdGet(sdRowsUrl(0)); SD.rows = r.items; sdTakePage(r); }
    else if (SD.filter === "gaps") { const r = await sdGet("/spaces/" + sp + "/gaps?open=true&limit=" + SD.limit); SD.rows = r.items; SD.total = r.total; }
    else SD.rows = await sdLearned();
  } catch(e) { SD.err = e.status === 404 ? sdNone() : e.message; }
  SD.busy = false; sdRender();
}
// GET /spaces/{space}/links/learned answers the learned links whose two ends the caller (or the Check as frame) reads,
// strongest first, with count, last confirmed and weight (count * 0.8^cycles since confirmed: what the per-row cap keeps)
async function sdLearned(){
  const r = await sdGet("/spaces/" + encodeURIComponent(SD.space) + "/links/learned?limit=" + SD.limit);
  SD.total = r.total; SD.truncated = r.total > r.items.length;
  return r.items.map(x => Object.assign({learned:true}, x));
}
const sdNum = n => (Math.round(Number(n || 0) * 100) / 100).toString();
function sdRowHtml(r, i){
  if (r.learned) return '<div class="kc-item sd-row' + (SD.sel === i ? " on" : "") + '" data-sdi="' + i + '"><b>' + esc(r.from_name || r.from) + ' → ' + esc(r.to_name || r.to) + '</b>'
    + (r.promote_candidate ? ' <span class="kc-tag task">promote?</span>' : '') + '<small class="kc-mono">' + esc(r.from) + ' → ' + esc(r.to) + '</small>' + (r.task ? '<small>task: ' + esc(r.task) + '</small>' : '')
    + '<small class="sd-lstat">count <i>' + sdNum(r.count) + '</i> · last confirmed <i>' + esc(r.last_confirmed ? when(r.last_confirmed) + " UTC" : "never") + '</i> · weight <i>' + sdNum(r.weight) + '</i>' + (r.contradictions ? ' · contradicted ' + r.contradictions + '×' : '') + '</small></div>';
  const kind = r.kind ? '<span class="kc-tag n">' + esc(r.kind) + '</span> ' : '', txt = String(r.text || r.task || r.key || "").slice(0, 160);
  return '<div class="kc-item sd-row' + (SD.sel === i ? " on" : "") + '" data-sdi="' + i + '"><b>' + kind + esc(txt || r.id) + '</b><small class="kc-mono">' + esc(r.id) + (r.ts ? ' · ' + esc(when(r.ts)) : '') + '</small></div>';
}
function sdDetailHtml(){
  const d = SD.det; if (!d) return '<p class="kc-hint">Click a row to open it: its core facts, links, sections and the rows it came from.</p>';
  if (d.busy) return '<p class="kc-hint">Opening…</p>';
  const r = d.row, x = d.exp, out = [];
  const qids = v => (Array.isArray(v) ? v : [v]).map(q => typeof q === "string" && q.startsWith(SD.space + "/") ? '<a class="sd-q kc-mono" data-sdq="' + esc(q) + '">' + esc(q) + '</a>' : '<span class="kc-mono">' + esc(typeof q === "string" ? q : JSON.stringify(q)) + '</span>').join("<br>");
  if (r.learned) { out.push('<h4>Learned link</h4><p class="kc-note"><b>' + esc(r.from_name || "") + '</b> → <b>' + esc(r.to_name || "") + '</b></p>' + row("From", qids(r.from)) + row("To", qids(r.to)) + (r.task ? row("Learned on the task", esc(r.task)) : '')
      + ((r.tasks || []).length > 1 ? row("Confirmed by the tasks", '<ul class="kc-ul">' + r.tasks.map(t => '<li>' + esc(t) + '</li>').join("") + '</ul>') : '')
      + row("Count", '<b>' + sdNum(r.count) + '</b> <small class="kc-hint">times a task used it; halves when not confirmed for 2 cycles</small>')
      + row("Last confirmed", esc(r.last_confirmed ? when(r.last_confirmed) + " UTC" : "never") + (r.first_ts ? '<br><small class="kc-hint">learned ' + esc(when(r.first_ts)) + '</small>' : ''))
      + row("Weight", '<b>' + sdNum(r.weight) + '</b> <small class="kc-hint">count × 0.8 per cycle since confirmed: what KH keeps when a row has over 16 learned links</small>')
      + row("Contradicted", String(r.contradictions || 0) + " time" + (r.contradictions === 1 ? "" : "s"))
      + row("Promote", r.promote_candidate ? '<span class="kc-tag task">candidate</span> <small class="kc-hint">the distiller may turn it into a typed link</small>' : '<span class="kc-hint">not yet (count ≥ 8 from ≥ 3 tasks, never contradicted)</span>')
      + (r.who ? row("Who", esc(r.who)) : '') + ((r.members || []).length ? row("Rolled up", qids(r.members)) : '')); return out.join(""); }
  if (x) { out.push('<h4>' + esc(x.name || x.id) + ' <span class="kc-tag n">' + esc(x.type || "") + '</span></h4>' + (x.summary ? '<p class="kc-note">' + esc(x.summary) + '</p>' : '')
      + row("Where", esc(x.where || "")) + ((x.core || []).length ? row("Core", '<ul class="kc-ul">' + x.core.map(c => '<li>' + esc(c) + '</li>').join("") + '</ul>') : '')
      + ((x.keys || []).length ? row("Keys", x.keys.map(k => '<span class="kc-tag n">' + esc(k) + '</span>').join(" ")) : '')
      + row("Links", (x.links || []).length ? x.links.map(l => '<div class="sd-link"><span class="kc-tag' + (l.kind === "learned" ? " task" : " n") + '">' + esc(l.kind) + (l.dir === "in" ? " ←" : " →") + '</span> <a class="sd-q" data-sdq="' + esc(l.to) + '">' + esc(l.name || l.to) + '</a> <small class="kc-hint">' + esc(l.where || "") + '</small></div>').join("") : '<span class="kc-hint">none</span>')
      + row("Sections", (Array.isArray(x.sections) ? x.sections : Object.keys(x.sections || {})).map(s => '<button class="btn g s" data-sdsec="' + esc(s) + '">' + esc(s) + '</button>').join(" ") + (d.sec ? '<div class="kc-pack sd-sec">' + esc(d.sec) + '</div>' : ''))
      + row("Came from", (x.src || []).length ? x.src.map(s => '<div><a class="sd-q kc-mono" data-sdq="' + esc(s.id) + '">' + esc(s.id) + '</a><br><small class="kc-hint">' + esc(s.text) + '</small></div>').join("") : '<span class="kc-hint">no source rows</span>')
      + ((x.history || []).length ? row("History", x.history.length + " older version(s)") : '')); }
  const skip = new Set(["text", "space", "scope", "qid", "sections", "src"]), fields = Object.keys(r).filter(k => !skip.has(k) && r[k] !== "" && r[k] != null);
  out.push('<h4>' + (x ? "The row's fields" : esc(r.kind ? r.kind + " " : "") + esc(r.id)) + '</h4>' + (r.text && !x ? '<div class="kc-pack sd-sec">' + esc(String(r.text).slice(0, 3000)) + '</div>' : '')
    + fields.map(k => row(k, Array.isArray(r[k]) || (typeof r[k] === "string" && r[k].startsWith(SD.space + "/")) ? qids(r[k]) : '<span class="kc-mono">' + esc(typeof r[k] === "object" ? JSON.stringify(r[k]) : r[k]) + '</span>')).join(""));
  if (d.err) out.push('<p class="kc-hint">' + esc(d.err) + '</p>');
  return out.join("");
}
function sdRender(){
  const el = sdRoot(); if (!el) return;
  const q = SD.q.trim().toLowerCase(), shown = SD.rows.map((r, i) => [r, i]).filter(([r]) => !q || JSON.stringify(r).toLowerCase().includes(q));
  const scopeSel = '<select id="sdScope"' + (SD.filter === "rows" ? "" : " disabled") + '>' + SD.scopes.map(x => '<option value="' + esc(x.scope) + '"' + (x.scope === SD.scope ? " selected" : "") + '>'
    + esc((SCOPE_LABEL[x.scope] || (SYS_SCOPES.find(y => y.scope === x.scope) || {}).name || x.scope) + " · " + (STORE_L[x.store] || x.store) + " · " + x.rows + " rows" + (x.system ? " · system" : "")) + '</option>').join("") + '</select>';
  el.innerHTML = '<div class="greet"><div><h1>Space Data</h1><p>The rows of a space you can read, scope by scope, read-only.</p></div></div>'
    + '<div class="card sd-bar"><div class="sd-f"><label>Space</label>' + pickField("sdsp", SD.space, "Pick a space…") + '</div>'
    + '<div class="sd-f"><label>Scope</label>' + scopeSel + '</div>'
    + '<div class="sd-f"><label>Check as</label><select id="sdAs" title="Sign-in off only: KH answers as this kind of caller would see it">' + SD_AS.map(([v, l]) => '<option value="' + v + '"' + (SD.as === v ? " selected" : "") + '>' + esc(l) + '</option>').join("") + '</select></div></div>'
    + '<div class="kc-pick sd-filters">' + SD_FILTERS.map(([k, l, dd]) => '<div data-sdf="' + k + '" class="' + (SD.filter === k ? "on" : "") + '"><b>' + esc(l) + '</b><small>' + esc(dd) + '</small></div>').join("") + '</div>'
    + '<section class="sd-cols"><div class="card kc-panel"><input type="text" class="kc-search" id="sdQ" placeholder="Search the rows shown" value="' + esc(SD.q) + '">'
    + (SD.err ? '<div class="kc-warn" id="sdErr">' + esc(SD.err) + '</div>' : SD.busy ? '<p class="kc-hint">Loading…</p>' : !SD.space ? '<p class="kc-hint">Pick a space first.</p>'
      : '<p class="kc-hint" id="sdCount">' + (SD.filter === "learned" ? shown.length + " learned link" + (shown.length === 1 ? "" : "s") + (SD.truncated ? " of " + SD.total + ", strongest first" : "") : (q ? shown.length + " of " : "") + SD.rows.length + " row" + (SD.rows.length === 1 ? "" : "s") + (SD.truncated ? " of " + SD.total + (SD.capped ? "+" : "") + " (newest first)" : "")) + '</p>'
        + '<div class="kc-src" id="sdList">' + (shown.length ? shown.map(([r, i]) => sdRowHtml(r, i)).join("") : '<p class="kc-hint">' + (SD.filter === "gaps" ? "No open gap: nothing waits for the distiller." : SD.filter === "learned" ? "No learned link yet." : "No row here.") + '</p>') + '</div>'
        + (SD.truncated && SD.filter !== "gaps" ? '<button class="btn g s" id="sdMore" style="align-self:flex-start">Show more</button>' : ''))
    + '</div><div class="card kc-panel" id="sdDet">' + sdDetailHtml() + '</div></section>';
  el.querySelector("[data-sdsp]").onclick = () => pickSpace(SD.space, async v => { SD.space = v; SD.scope = ""; SD.limit = 50; SD.q = ""; await sdScopes(); sdLoad(); }, {noCtx:true});
  $("sdScope").onchange = e => { SD.scope = e.target.value; SD.limit = 50; sdLoad(); };
  $("sdAs").onchange = async e => { SD.as = e.target.value; SD.err = ""; await sdScopes(); sdLoad(); };
  el.querySelectorAll("[data-sdf]").forEach(x => x.onclick = () => { SD.filter = x.dataset.sdf; SD.limit = 50; SD.q = ""; if (!SD.err) sdLoad(); else sdRender(); });
  const qi = $("sdQ"); qi.oninput = () => { SD.q = qi.value; sdRender(); const n = $("sdQ"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); };
  const mo = $("sdMore"); if (mo) mo.onclick = () => { if (SD.filter === "rows" || SD.filter === "cases") return sdPage(); SD.limit = Math.min(500, SD.limit + 50); sdLoad(); };
  el.querySelectorAll("[data-sdi]").forEach(x => x.onclick = () => sdOpen(SD.rows[+x.dataset.sdi], +x.dataset.sdi));
  el.querySelectorAll("[data-sdq]").forEach(x => x.onclick = () => sdOpen({id:x.dataset.sdq, qid:x.dataset.sdq}, null));
  el.querySelectorAll("[data-sdsec]").forEach(x => x.onclick = async () => { const d = SD.det; try { const r = await sdGet("/spaces/" + encodeURIComponent(SD.space) + "/rows/" + encodeURI(d.exp.id) + "?section=" + encodeURIComponent(x.dataset.sdsec));
    d.sec = (r.sections || {})[x.dataset.sdsec] || "(empty)"; } catch(e) { d.sec = e.message; } sdRender(); });
}
async function sdOpen(r, i){
  SD.sel = i; SD.det = {row:r, busy:!r.learned}; sdRender(); if (r.learned) return;
  const q = r.qid || r.id; let exp = null, err = "";
  if (r.scope !== "index" && r.scope !== "events" && typeof q === "string" && q.startsWith(SD.space + "/")) {
    try { exp = await sdGet("/spaces/" + encodeURIComponent(SD.space) + "/rows/" + encodeURI(q)); } catch(e) { err = e.status === 404 ? "" : e.message; } }
  if (!exp && !r.scope) err = err || sdNone();
  SD.det = {row:r, exp, err}; sdRender();
}
// the Waiting runs count on the Flow Config nav entry, from page load on; refreshed every 30 s while the page shows
setTimeout(() => refreshWaiting().catch(() => {}), 800);
setInterval(() => { if (document.hidden) return; refreshWaiting().then(() => { const w = $("ffWait"); if (w) { w.outerHTML = waitHtml(); wireWait(); } }).catch(() => {}); }, 30000);
})();
// The page may open on #space or #flows before this file loaded: start now if one of them is showing.
["space", "flows"].forEach(v => { const el = document.getElementById("v-" + v); if (el && !el.classList.contains("hide")) window.loadFlows(v === "space" ? "space" : "flow"); });
(el => { if (el && !el.classList.contains("hide")) window.loadSpaceData(); })(document.getElementById("v-spacedata"));
