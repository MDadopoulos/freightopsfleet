"""The chat page — one screen in front of ADK's own API.

ADK's dev UI is a developer tool: an agent picker, an eval tab, a trace viewer,
a settings drawer. A judge wants to ask the fleet a question and watch it work.
This page is that and nothing else, served at `/chat` by the same app that
serves the API, so the login and the gate cover it without a single new rule:
the browser calls `/run_sse` and the sessions endpoints on the same origin, the
cookie rides along, and `user_id` is pinned before ADK sees it.

It is the ONE place this project ships JavaScript, and it says so. The console
stays zero-JS because it renders a record; a streaming conversation cannot be
a form post. The script is small, framework-free and inlined — no CDN, no
build step, no external request — and every string from the model is escaped
before it touches the DOM.

What the page shows besides the conversation, because a judge asked for it:
the routing and every tool call as it happens (the trace, inline), the token
usage of each turn and of the session, and an upload box that puts a PDF or a
scan into the fleet's inbox through the same `ingest` step the operator runs.

The page wears the console's CSS and nav so a visitor never learns two layouts.
The eight starter questions are the README's, in the README's order; keep the
two in step.
"""

from __future__ import annotations

import html
import json
import os

APP_NAME = "freight_ops"

#: The README's "Eight questions worth asking", verbatim.
STARTERS = [
    "Cross-check shipment shp-002-hero and list every discrepancy with the document each figure comes from",
    "Is shp-001-pristine clean?",
    "Sort the documents in inbox/ into shipment sets and tell me what is missing",
    "Compare the two quotes in quotes/ against the freight invoice for shp-004-quote-invoice",
    "Which shipment is missing a commercial invoice, and draft the chaser",
    "Read raw/inbox/scan_001.pdf",
    "Check the container numbers on shp-003-container-refs",
    "What did you tell me last time about shp-002-hero?",
]

_CSS = """
html,body{height:100%}body{display:flex;flex-direction:column}
.nav .in{max-width:1180px}
.main{flex:1;max-width:1180px;width:100%;margin-inline:auto;display:grid;grid-template-columns:270px 1fr;gap:20px;padding:20px;min-height:0;box-sizing:border-box}
.side{border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:14px;align-self:start;position:sticky;top:76px}
.side h2{font:600 15px/1.3 var(--display);margin:0 0 8px}
.side .label{display:block;margin:14px 0 6px;color:var(--ink-3)}
.side button,.side a.sess{display:block;width:100%;text-align:left;border:1px solid var(--line);background:var(--bg);color:var(--ink);
border-radius:8px;padding:8px 10px;margin:0 0 6px;font:500 13.5px/1.35 var(--sans);cursor:pointer;text-decoration:none;box-sizing:border-box}
.side a.sess.current{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.side button.new{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:700}
.side input[type=file]{width:100%;font:13px var(--sans);margin:0 0 6px}
.side .who{font:600 13.5px/1.35 var(--sans);color:var(--ink-2);margin:0 0 6px;overflow-wrap:anywhere}
.usage{font:500 13px/1.5 var(--sans);color:var(--ink-2)}.usage b{color:var(--ink);font-variant-numeric:tabular-nums}
.chat{display:flex;flex-direction:column;min-height:70vh}
.log{flex:1;display:flex;flex-direction:column;gap:12px;padding-bottom:16px}
.welcome{border:1px solid var(--line);border-left:6px solid var(--accent);border-radius:12px;background:var(--surface);padding:20px}
.welcome h1{font:650 30px/1.15 var(--display);letter-spacing:-.01em;margin:0 0 8px}
.welcome p{margin:0 0 10px;color:var(--ink-2);max-width:68ch}
.starters{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.starters button{border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:999px;padding:8px 12px;
font:500 13.5px/1.3 var(--sans);cursor:pointer;text-align:left;max-width:100%}
.starters button:hover{border-color:var(--accent)}
.msg{max-width:78ch;border-radius:12px;padding:12px 16px;white-space:pre-wrap;overflow-wrap:anywhere}
.msg.user{align-self:flex-end;background:var(--accent);color:var(--accent-ink)}
.msg.bot{align-self:flex-start;background:var(--surface);border:1px solid var(--line)}
.msg.bot.thinking{color:var(--ink-3);font-style:italic}
.ev{margin:10px -16px -12px;padding:8px 16px;border-top:1px solid var(--line);font:500 12.5px/1.5 var(--sans);color:var(--ink-3);white-space:normal}.ev b{color:var(--ink-2)}.ev a{font-family:var(--mono);font-size:12px}
.card{align-self:flex-start;max-width:78ch;border-radius:10px;padding:10px 14px;font:500 13.5px/1.45 var(--sans);border-left:5px solid currentColor;background:var(--surface)}
.card.route{color:var(--ink-3);background:var(--sunk)}
.card.held{color:var(--held);background-color:var(--held-tint)}.card.held b{color:var(--ink)}
.card.done{color:var(--executed);background-color:var(--executed-tint)}
.card.err{color:var(--blocked);background-color:var(--blocked-tint)}
.card .mono{font-family:var(--mono);font-size:12.5px}
.trace{align-self:flex-start;max-width:78ch;font:500 12.5px/1.5 var(--sans);color:var(--ink-3)}
.trace summary{cursor:pointer}.trace table{border-collapse:collapse;margin-top:6px}.trace td{padding:2px 10px 2px 0;vertical-align:top;font-family:var(--mono);font-size:12px}
.compose{position:sticky;bottom:0;background:var(--bg);padding:12px 0 4px;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;min-height:56px;max-height:200px;resize:vertical;border:1px solid var(--line);border-radius:12px;padding:14px 16px;
font:400 16px/1.45 var(--sans);background:var(--surface);color:var(--ink)}
.send{min-height:56px;padding:0 22px;border-radius:12px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);
font:700 15px/1 var(--sans);cursor:pointer}.send:disabled{opacity:.55;cursor:wait}
.chat .foot{color:var(--ink-3);font-size:13px;margin:6px 0 0;max-width:78ch;border:0;padding:0}
@media (max-width:820px){.main{grid-template-columns:1fr}.side{position:static}.msg{max-width:100%}}
@media (prefers-reduced-motion:no-preference){.msg,.card{animation:rise .35s ease backwards}@keyframes rise{from{opacity:0;transform:translateY(6px)}}}
"""

_JS = r"""
const APP = document.body.dataset.app, DESK = document.body.dataset.desk || '/desk';
const PRICES = JSON.parse(document.body.dataset.prices || 'null');
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const log = document.getElementById('log'), box = document.getElementById('box'), send = document.getElementById('send');
const sessBox = document.getElementById('sessions'), usageBox = document.getElementById('usage');
let sessionId = null, busy = false;
let reads = [];  // documents the fleet read during the current turn
const usage = { turns: 0, inTok: 0, outTok: 0 };

function md(text) {
  let s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  return s;
}
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function bubble(kind, text) { const m = el('div', 'msg ' + kind); m.innerHTML = md(text || ''); log.appendChild(m); scroll(); return m; }
function card(kind, html) { const c = el('div', 'card ' + kind); c.innerHTML = html; log.appendChild(c); scroll(); return c; }
function scroll() { window.scrollTo(0, document.body.scrollHeight); }

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  if (r.status === 403 || r.status === 401) { location.href = '/access?next=' + encodeURIComponent('/chat'); throw new Error('login'); }
  if (!r.ok) throw new Error(r.status + ' ' + (await r.text()).slice(0, 200));
  return r;
}
const base = () => `/apps/${APP}/users/me/sessions`;

function digest(args) {
  const keep = ['path', 'prefix', 'pattern', 'to', 'subject', 'container_number'];
  const out = [];
  for (const k of Object.keys(args || {})) {
    if (keep.includes(k)) out.push(k + '=' + String(args[k]).slice(0, 80));
    else if (typeof args[k] === 'string') out.push(k + '=' + args[k].length + ' chars');
  }
  return out.join(' · ');
}

function renderUsage() {
  let cost = '';
  if (PRICES && (usage.inTok || usage.outTok)) {
    const usd = usage.inTok / 1e6 * PRICES[0] + usage.outTok / 1e6 * PRICES[1];
    cost = ' · ≈ <b>$' + usd.toFixed(4) + '</b> at the configured rates';
  } else if (!PRICES) cost = ' · price it by setting FREIGHT_PRICE_IN_PER_M / OUT_PER_M';
  usageBox.innerHTML = '<b>' + usage.turns + '</b> turn' + (usage.turns === 1 ? '' : 's') + ' this visit · tokens in <b>' + usage.inTok.toLocaleString() + '</b> · out <b>' + usage.outTok.toLocaleString() + '</b>' + cost + '<br>Each desk declares a cap of $0.50 per run in the catalog; the model quota is the hard ceiling.';
}

async function listSessions() {
  const r = await api(base());
  const all = await r.json();
  sessBox.innerHTML = '';
  all.sort((a, b) => (b.lastUpdateTime || 0) - (a.lastUpdateTime || 0)).slice(0, 12).forEach(s => {
    const a = el('a', 'sess' + (s.id === sessionId ? ' current' : ''), '');
    a.href = '#' + s.id;
    const when = s.lastUpdateTime ? new Date(s.lastUpdateTime * 1000).toLocaleString() : '';
    a.textContent = (s.id === sessionId ? '● ' : '') + when;
    a.onclick = (e) => { e.preventDefault(); open(s.id); };
    sessBox.appendChild(a);
  });
  if (!all.length) sessBox.appendChild(el('div', 'foot', 'No conversations yet.'));
}

// One trace per turn: every event's author and what it did, as a table a judge can open.
let trace = null;
function traceRow(ev, what) {
  if (!trace) return;
  const tr = el('tr'); const t = new Date().toLocaleTimeString();
  [t, ev.author || '', what].forEach(v => tr.appendChild(el('td', '', v)));
  trace.querySelector('tbody').appendChild(tr);
}
function newTrace() {
  const d = el('details', 'trace'); d.innerHTML = '<summary>Trace — who did what, in order</summary><table><tbody></tbody></table>';
  log.appendChild(d); trace = d; return d;
}

function renderEvent(ev, live) {
  const parts = (ev.content && ev.content.parts) || [];
  for (const p of parts) {
    if (p.functionCall) {
      const fc = p.functionCall; const d = digest(fc.args);
      traceRow(ev, 'call ' + fc.name + (d ? ' (' + d + ')' : ''));
      if (fc.name === 'read_file' && fc.args && fc.args.path && !reads.includes(fc.args.path)) reads.push(fc.args.path);
      if (live) card('route', '→ <b>' + esc(ev.author || 'fleet') + '</b> calls <span class="mono">' + esc(fc.name) + '</span>' + (d ? ' <span class="mono">' + esc(d) + '</span>' : ''));
    } else if (p.functionResponse) {
      const resp = p.functionResponse.response || {};
      const st = resp.status || (resp.result && resp.result.status);
      const name = p.functionResponse.name || '';
      traceRow(ev, 'result ' + name + ' → ' + (st || 'ok'));
      if (st === 'pending_approval') {
        const id = resp.approval_id || (resp.result && resp.result.approval_id) || '';
        card('held', '<b>HELD for a human.</b> <span class="mono">' + esc(name) + '</span> is consequential; the fleet drafted it and stopped. Approval id <span class="mono">' + esc(id) + '</span>. <a href="' + esc(DESK) + '/decision/' + encodeURIComponent(id) + '">Approve or reject it on the desk →</a>');
      } else if (st === 'blocked') card('err', '<b>BLOCKED.</b> ' + esc(resp.message || 'the gate refused this call'));
      else if (st === 'binary') card('route', 'read_file refused a binary: the fleet does not pretend to have read a PDF it cannot. Upload it on the left to have it transcribed.');
    } else if (p.text && !live) {
      if ((ev.content.role || 'model') === 'user') bubble('user', p.text); else bubble('bot', p.text);
    }
  }
  if (!live && ev.usageMetadata) tally(ev);
}

const seenUsage = new Set();
function tally(ev) {
  const u = ev.usageMetadata; if (!u || ev.partial) return;
  const key = ev.id || (ev.timestamp + ':' + (u.totalTokenCount || 0)); if (seenUsage.has(key)) return; seenUsage.add(key);
  usage.inTok += u.promptTokenCount || 0; usage.outTok += (u.candidatesTokenCount || 0) + (u.thoughtsTokenCount || 0);
  renderUsage();
}

async function open(id) {
  sessionId = id; log.innerHTML = ''; trace = null;
  const r = await api(base() + '/' + encodeURIComponent(id));
  const s = await r.json();
  (s.events || []).forEach(ev => renderEvent(ev, false));
  if (!(s.events || []).length) welcome();
  listSessions();
}

async function fresh() {
  const id = 'chat-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
  await api(base() + '/' + id, { method: 'POST', body: '{}' });
  sessionId = id; log.innerHTML = ''; trace = null; welcome(); listSessions();
}

function welcome() {
  const w = el('div', 'welcome');
  w.innerHTML = '<h1>Ask the fleet</h1><p>A governed fleet of freight back-office agents: it reads the shipment documents in its workspace, cross-checks them, drafts notices — and every consequential action stops at a policy gate for a human. Ask about a <em>named</em> shipment or folder; the starters below are the ones that make the argument. Ask it to <em>send</em> a notice and watch the hold appear on the desk.</p>';
  const st = el('div', 'starters');
  JSON.parse(document.getElementById('starters').textContent).forEach(q => { const b = el('button', '', q); b.onclick = () => { box.value = q; ask(); }; st.appendChild(b); });
  w.appendChild(st); log.appendChild(w);
}

async function ask() {
  const text = box.value.trim(); if (!text || busy) return;
  if (!sessionId) await fresh();
  busy = true; send.disabled = true; box.value = '';
  bubble('user', text);
  const think = bubble('bot thinking', 'The fleet is working — a cross-check reads three documents and can take a minute…');
  let bot = null, acc = ''; newTrace(); usage.turns += 1; renderUsage(); reads = [];
  try {
    const r = await api('/run_sse', { method: 'POST', body: JSON.stringify({ app_name: APP, user_id: 'me', session_id: sessionId, new_message: { role: 'user', parts: [{ text }] }, streaming: true }) });
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue;
          let ev; try { ev = JSON.parse(line.slice(5)); } catch { continue; }
          const parts = (ev.content && ev.content.parts) || [];
          const txt = parts.filter(p => p.text).map(p => p.text).join('');
          if (parts.some(p => p.functionCall || p.functionResponse)) { think.remove(); renderEvent(ev, true); }
          if (txt) {
            think.remove();
            if (!bot) bot = bubble('bot', '');
            if (ev.partial) { acc += txt; bot.innerHTML = md(acc); } else { acc = txt; bot.innerHTML = md(acc); }
            scroll();
          }
          if (!ev.partial && ev.usageMetadata) tally(ev);
          if (ev.errorMessage) { think.remove(); card('err', esc(ev.errorMessage)); traceRow(ev, 'error ' + ev.errorMessage.slice(0, 120)); }
        }
      }
    }
    if (!bot) { think.remove(); card('route', 'The fleet finished without a reply to show. Try one of the starters.'); }
    if (bot) evidence(bot);
  } catch (e) { think.remove(); if (e.message !== 'login') card('err', 'Request failed: ' + esc(e.message)); }
  busy = false; send.disabled = false; box.focus(); listSessions();
}

function evidence(bot) {
  const e = el('div', 'ev');
  if (!reads.length) { e.innerHTML = '<b>Evidence:</b> no document was read for this answer — treat it as routing or recall, not a finding.'; }
  else e.innerHTML = '<b>Evidence — ' + reads.length + ' document' + (reads.length === 1 ? '' : 's') + ' read:</b> ' + reads.map(p => '<a href="/doc?path=' + encodeURIComponent(p) + '">' + esc(p) + '</a>').join(' · ');
  bot.appendChild(e);
}

async function upload() {
  const input = document.getElementById('file'), status = document.getElementById('upstatus');
  const f = input.files && input.files[0]; if (!f) { status.textContent = 'Choose a PDF, PNG or JPEG first.'; return; }
  status.textContent = 'Uploading and transcribing — the model reads the page; give it ~20 s…';
  try {
    const r = await fetch('/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f });
    const j = await r.json();
    if (!r.ok || j.status !== 'ok') { status.textContent = 'Not ingested: ' + (j.message || r.status); return; }
    status.textContent = 'In the inbox as ' + j.inbox + ' (' + j.chars + ' chars).';
    if (!sessionId) await fresh();
    const q = 'Read ' + j.inbox + ' and tell me which shipment it belongs to and whether anything in it disagrees with the documents we already hold.';
    card('done', '<b>Uploaded.</b> <span class="mono">' + esc(j.raw) + '</span> was transcribed to <span class="mono">' + esc(j.inbox) + '</span> by the same ingest step the operator runs. <a href="#" id="askup">Ask the fleet about it →</a>');
    document.getElementById('askup').onclick = (e) => { e.preventDefault(); box.value = q; ask(); };
    input.value = '';
  } catch (e) { status.textContent = 'Upload failed: ' + e.message; }
}

send.onclick = ask;
box.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } });
document.getElementById('new').onclick = fresh;
document.getElementById('up').onclick = upload;
renderUsage();
(async () => { try { await listSessions(); const h = location.hash.slice(1); if (h) await open(h); else await fresh(); } catch (e) { if (e.message !== 'login') card('err', esc(e.message)); } })();
"""


def _prices() -> str:
    """`[in, out]` USD per million tokens from env, or empty — the page never
    invents a price."""
    try:
        i = float(os.environ.get("FREIGHT_PRICE_IN_PER_M", ""))
        o = float(os.environ.get("FREIGHT_PRICE_OUT_PER_M", ""))
    except ValueError:
        return ""
    return json.dumps([i, o])


def page(identity: str = "") -> str:
    """The chat page, rendered from env at request time like the console's
    pages, wearing the console's CSS and nav. `identity` is what the login
    layer says the visitor is; the page only ever shows it back to them."""
    from .console import CSS, load_pending, nav_html, stranded_count

    try:
        pending = len(load_pending())
        stranded = stranded_count()
    except Exception:  # noqa: BLE001 - a broken store must not take the chat down with it
        pending, stranded = 0, 0
    who = (f'<p class="who">You are <span class="mono">{html.escape(identity)}</span>. Conversations, '
           "uploads and decisions are recorded under that name.</p>" if identity else "")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Ask the fleet</title><style>{CSS}{_CSS}</style></head>"
        f'<body data-app="{APP_NAME}" data-desk="/desk" data-prices="{html.escape(_prices(), quote=True)}">'
        + nav_html(active="chat", pending=pending, stranded=stranded)
        + '<div class="main"><aside class="side"><h2>Your conversations</h2>'
        + who
        + '<button class="new" id="new" type="button">+ New conversation</button>'
        '<span class="label">Recent</span><div id="sessions"></div>'
        '<span class="label">Upload a document</span>'
        '<input type="file" id="file" accept=".pdf,.png,.jpg,.jpeg">'
        '<button id="up" type="button">Transcribe into the inbox</button>'
        '<p class="foot" id="upstatus">A PDF or a scan, up to 6 MB. It is read by the model into markdown and '
        "lands in the fleet's inbox, where the agents can read it.</p>"
        '<span class="label">This visit</span><div class="usage" id="usage"></div>'
        '<p class="foot">Conversations are yours alone and survive a reload — they live in a database, not this tab. '
        '<a href="/dev-ui/">ADK\'s own trace viewer</a> has the raw events.</p></aside>'
        '<section class="chat"><div class="log" id="log"></div>'
        '<div class="compose"><textarea id="box" placeholder="Ask about a named shipment, e.g. “Is shp-001-pristine clean?”"></textarea>'
        '<button class="send" id="send" type="button">Ask</button></div>'
        '<p class="foot">Every shipment here is fictional. A hold raised here is the same hold the '
        '<a href="/desk">desk</a> approves or rejects. This page is the one place the project runs JavaScript, inlined and '
        "framework-free.</p></section></div>"
        f'<script type="application/json" id="starters">{json.dumps(STARTERS)}</script>'
        f"<script>{_JS}</script></body></html>"
    )
