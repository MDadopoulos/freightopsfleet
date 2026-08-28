"""The chat page — one screen in front of ADK's own API.

ADK's dev UI is a developer tool: an agent picker, an eval tab, a trace viewer,
a settings drawer. A judge wants to ask the fleet a question and watch it work.
This page is that and nothing else, served at `/chat` by the same app that
serves the API, so the identity middleware and the gate cover it without a
single new rule: the browser calls `/run_sse` and the sessions endpoints on the
same origin, cookies ride along, and `user_id` is pinned before ADK sees it.

It is the ONE place this project ships JavaScript, and it says so. The console
stays zero-JS because it renders a record; a streaming conversation cannot be
a form post. The script is small, framework-free and inlined — no CDN, no
build step, no external request — and every string from the model is escaped
before it touches the DOM.

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
:root{--bg:#F7F6F3;--surface:#FFFFFF;--sunk:#EFEDE8;--line:#DCD9D2;--ink:#16181D;--ink-2:#454B54;
--ink-3:#5F6670;--accent:#0B5FFF;--accent-ink:#FFFFFF;--held:#8A5205;--held-tint:#FDF0D5;
--executed:#166534;--executed-tint:#DEF3E5;--blocked:#A31515;--blocked-tint:#FDE4E4;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
--display:"Iowan Old Style","Palatino Linotype",Palatino,Charter,Georgia,serif}
@media (prefers-color-scheme:dark){:root{--bg:#14161A;--surface:#1C1F25;--sunk:#101215;--line:#2C313A;
--ink:#F3F4F6;--ink-2:#C3C8D1;--ink-3:#98A0AC;--accent:#7EA6FF;--accent-ink:#0F1319;--held:#F5B840;
--held-tint:#3A2C10;--executed:#5BD79A;--executed-tint:#12301F;--blocked:#FCA5A5;--blocked-tint:#3A1A18}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:400 16.5px/1.5 var(--sans);display:flex;flex-direction:column}
a{color:var(--accent)}code{font-family:var(--mono);font-size:.92em;background:var(--sunk);padding:1px 5px;border-radius:4px}
.nav{background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.nav::after{content:"";display:block;height:3px;opacity:.9;background:linear-gradient(90deg,var(--held) 0 22%,#1E40AF 22% 42%,var(--executed) 42% 62%,#3E4A5A 62% 81%,var(--blocked) 81% 100%)}
.nav .in{max-width:1180px;margin-inline:auto;padding:0 20px;min-height:56px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand{font:700 12.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);white-space:nowrap}
.brand strong{color:var(--ink)}
.links{margin-left:auto;display:flex;gap:4px;flex-wrap:wrap}
.links a{display:inline-flex;align-items:center;min-height:36px;padding:0 10px;border-radius:8px;font:600 13.5px/1 var(--sans);
color:var(--ink-2);text-decoration:none}.links a:hover{background:var(--sunk)}
.main{flex:1;max-width:1180px;width:100%;margin-inline:auto;display:grid;grid-template-columns:260px 1fr;gap:20px;padding:20px;min-height:0}
.side{border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:14px;align-self:start;position:sticky;top:76px}
.side h2{font:600 15px/1.3 var(--display);margin:0 0 8px}
.side .label{font:700 11.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);display:block;margin:12px 0 6px}
.side button,.side a.sess{display:block;width:100%;text-align:left;border:1px solid var(--line);background:var(--bg);color:var(--ink);
border-radius:8px;padding:8px 10px;margin:0 0 6px;font:500 13.5px/1.35 var(--sans);cursor:pointer;text-decoration:none}
.side a.sess.current{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.side button.new{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:700}
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
.card{align-self:flex-start;max-width:78ch;border-radius:10px;padding:10px 14px;font:500 13.5px/1.45 var(--sans);border-left:5px solid currentColor}
.card.route{color:var(--ink-3);background:var(--sunk)}
.card.held{color:var(--held);background-color:var(--held-tint)}.card.held b{color:var(--ink)}
.card.done{color:var(--executed);background-color:var(--executed-tint)}
.card.err{color:var(--blocked);background-color:var(--blocked-tint)}
.card .mono{font-family:var(--mono);font-size:12.5px}
.compose{position:sticky;bottom:0;background:var(--bg);padding:12px 0 4px;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;min-height:56px;max-height:200px;resize:vertical;border:1px solid var(--line);border-radius:12px;padding:14px 16px;
font:400 16px/1.45 var(--sans);background:var(--surface);color:var(--ink)}
.send{min-height:56px;padding:0 22px;border-radius:12px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);
font:700 15px/1 var(--sans);cursor:pointer}.send:disabled{opacity:.55;cursor:wait}
.foot{color:var(--ink-3);font-size:13px;margin:6px 0 0;max-width:78ch}
@media (max-width:820px){.main{grid-template-columns:1fr}.side{position:static}.msg{max-width:100%}}
@media (prefers-reduced-motion:no-preference){.msg,.card{animation:rise .35s ease backwards}@keyframes rise{from{opacity:0;transform:translateY(6px)}}}
"""

_JS = r"""
const APP = document.body.dataset.app;
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const log = document.getElementById('log'), box = document.getElementById('box'), send = document.getElementById('send');
const sessBox = document.getElementById('sessions');
let sessionId = null, busy = false;

function md(text) {
  let s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  return s;
}
function bubble(kind, text) { const m = el('div', 'msg ' + kind); m.innerHTML = md(text || ''); log.appendChild(m); scroll(); return m; }
function card(kind, html) { const c = el('div', 'card ' + kind); c.innerHTML = html; log.appendChild(c); scroll(); return c; }
function scroll() { window.scrollTo(0, document.body.scrollHeight); }
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  if (r.status === 403 || r.status === 401) { location.href = '/access?next=' + encodeURIComponent('/chat'); throw new Error('login'); }
  if (!r.ok) throw new Error(r.status + ' ' + (await r.text()).slice(0, 200));
  return r;
}
const base = () => `/apps/${APP}/users/me/sessions`;

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

function renderEvent(ev, live) {
  const parts = (ev.content && ev.content.parts) || [];
  for (const p of parts) {
    if (p.functionCall) card('route', '→ handing to <b>' + esc(p.functionCall.name) + '</b>');
    else if (p.functionResponse) {
      const resp = p.functionResponse.response || {};
      const st = resp.status || (resp.result && resp.result.status);
      if (st === 'pending_approval') card('held', '<b>HELD for a human.</b> The fleet drafted but did not write or send. Approval id <span class="mono">' + esc(resp.approval_id || (resp.result && resp.result.approval_id) || '') + '</span>.' + (document.body.dataset.sandbox ? ' To press approve for real, use <a href="' + esc(document.body.dataset.sandbox) + '">the sandbox</a>.' : ''));
      else if (st === 'binary') card('route', 'read_file refused a binary: the fleet does not pretend to have read a PDF it cannot. <span class="mono">' + esc(JSON.stringify(resp).slice(0, 160)) + '</span>');
    } else if (p.text && !live) {
      if ((ev.content.role || 'model') === 'user') bubble('user', p.text); else bubble('bot', p.text);
    }
  }
}

async function open(id) {
  sessionId = id; log.innerHTML = '';
  const r = await api(base() + '/' + encodeURIComponent(id));
  const s = await r.json();
  (s.events || []).forEach(ev => renderEvent(ev, false));
  if (!(s.events || []).length) welcome();
  listSessions();
}

async function fresh() {
  const id = 'chat-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
  await api(base() + '/' + id, { method: 'POST', body: '{}' });
  sessionId = id; log.innerHTML = ''; welcome(); listSessions();
}

function welcome() {
  const w = el('div', 'welcome');
  w.innerHTML = '<h1>Ask the fleet</h1><p>A governed fleet of freight back-office agents: it reads the shipment documents in its workspace, cross-checks them, drafts notices — and every consequential action stops at a policy gate for a human. Ask about a <em>named</em> shipment or folder; the starters below are the ones that make the argument.</p>';
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
  let bot = null, acc = '';
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
          if (ev.errorMessage) { think.remove(); card('err', esc(ev.errorMessage)); }
        }
      }
    }
    if (!bot) { think.remove(); card('route', 'The fleet finished without a reply to show. Try one of the starters.'); }
  } catch (e) { think.remove(); if (e.message !== 'login') card('err', 'Request failed: ' + esc(e.message)); }
  busy = false; send.disabled = false; box.focus(); listSessions();
}

send.onclick = ask;
box.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } });
document.getElementById('new').onclick = fresh;
(async () => { try { await listSessions(); const h = location.hash.slice(1); if (h) await open(h); else await fresh(); } catch (e) { if (e.message !== 'login') card('err', esc(e.message)); } })();
"""


def page() -> str:
    """The chat page, rendered from env at request time like the console's pages."""
    public = os.environ.get("FREIGHT_PUBLIC_URL", "").strip()
    sandbox = os.environ.get("FREIGHT_SANDBOX_URL", "").strip()
    links = "".join(
        f'<a href="{html.escape(u, quote=True)}">{t}</a>'
        for t, u in (("Public desk", public), ("Sandbox", sandbox))
        if u
    ) + '<a href="/dev-ui/" title="ADK\'s developer UI: traces and events">Trace view</a>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Ask the fleet</title><style>{_CSS}</style></head>"
        f'<body data-app="{APP_NAME}" data-sandbox="{html.escape(sandbox, quote=True)}">'
        '<div class="nav"><div class="in"><span class="brand">Freight Ops Fleet · <strong>Ask the fleet</strong></span>'
        f'<span class="links">{links}</span></div></div>'
        '<div class="main"><aside class="side"><h2>Your conversations</h2>'
        '<button class="new" id="new" type="button">+ New conversation</button>'
        '<span class="label">Recent</span><div id="sessions"></div>'
        '<p class="foot">Conversations are yours alone and survive a reload — they live in a database, not this tab.</p></aside>'
        '<section class="chat"><div class="log" id="log"></div>'
        '<div class="compose"><textarea id="box" placeholder="Ask about a named shipment, e.g. “Is shp-001-pristine clean?”"></textarea>'
        '<button class="send" id="send" type="button">Ask</button></div>'
        '<p class="foot">Every shipment here is fictional. Holds raised in chat show up here as HELD and are not the governed record; '
        "the sandbox is where you press approve. This page is the one place the project runs JavaScript, inlined and "
        "framework-free.</p></section></div>"
        f'<script type="application/json" id="starters">{json.dumps(STARTERS)}</script>'
        f"<script>{_JS}</script></body></html>"
    )
