"""The Control Center page: "Build something…", and what happened next.

A single self-contained page, served by the API, for the same reason the
reliability dashboard is one: the React app under ``frontend/`` needs a build
step before a new page exists, and a page that requires a Node toolchain to
appear is a page an operator running from a source checkout does not have.

Two screens' worth of content and no framework. The prominent thing is the
input, because §21 says so and because the whole product is that sentence: the
operator types "add a weekly coach summary" and watches it happen. Everything
below the input is the answer to "what is happening, and can I trust it" —
progress in the pipeline's own words, the acceptance contract in the operator's,
the attempts with what each one got wrong, and the approvals that are waiting.

Nothing here decides anything. Every action is a POST to a route that goes
through the authority model, and every refusal is rendered as the sentence Wiz
returned rather than as an error.
"""

from __future__ import annotations

try:
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
except ImportError:  # pragma: no cover - server extra not installed
    raise ImportError("fastapi is required for the Wiz Control Center")

router = APIRouter(tags=["wiz"])

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wiz — Build something</title>
<style>
  :root {
    --bg:#ffffff; --fg:#14161a; --muted:#6b7280; --line:#e5e7eb; --card:#f9fafb;
    --ok:#15803d; --warn:#b45309; --down:#b91c1c; --accent:#1d4ed8;
    --accent-soft:#eff6ff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0f1115; --fg:#e6e8eb; --muted:#9aa1ab; --line:#262b33; --card:#161a21;
      --ok:#4ade80; --warn:#fbbf24; --down:#f87171; --accent:#60a5fa;
      --accent-soft:#15223a;
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; padding:2rem 1.25rem; background:var(--bg); color:var(--fg);
    font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width:860px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 .2rem; letter-spacing:.02em; }
  .sub { color:var(--muted); margin:0 0 1.75rem; font-size:.9rem; }
  h2 { font-size:.78rem; text-transform:uppercase; letter-spacing:.09em;
       color:var(--muted); margin:2.25rem 0 .75rem; font-weight:600; }

  .ask { display:flex; gap:.6rem; align-items:stretch; }
  .ask input {
    flex:1; font:inherit; font-size:1.05rem; padding:.85rem 1rem;
    border:1px solid var(--line); border-radius:10px; background:var(--card);
    color:var(--fg);
  }
  .ask input:focus { outline:2px solid var(--accent); outline-offset:1px; }
  button {
    font:inherit; font-weight:600; padding:.85rem 1.2rem; border-radius:10px;
    border:1px solid var(--accent); background:var(--accent); color:#fff;
    cursor:pointer;
  }
  button.quiet { background:transparent; color:var(--fg); border-color:var(--line); }
  button:disabled { opacity:.5; cursor:default; }
  .hint { color:var(--muted); font-size:.85rem; margin:.6rem 0 0; }

  .banner { border:1px solid var(--line); border-radius:10px; padding:.8rem 1rem;
            margin:1rem 0 0; background:var(--card); }
  .banner.warn { border-color:var(--warn); }
  .banner.bad { border-color:var(--down); }

  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:1rem 1.1rem; margin-bottom:.75rem; }
  .card h3 { margin:0 0 .3rem; font-size:1rem; }
  .row { display:flex; gap:.6rem; align-items:center; flex-wrap:wrap; }
  .spacer { flex:1; }
  .pill { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em;
          padding:.15rem .5rem; border-radius:999px; border:1px solid var(--line);
          color:var(--muted); }
  .pill.ok { color:var(--ok); border-color:var(--ok); }
  .pill.warn { color:var(--warn); border-color:var(--warn); }
  .pill.bad { color:var(--down); border-color:var(--down); }
  .pill.busy { color:var(--accent); border-color:var(--accent);
               background:var(--accent-soft); }
  .said { color:var(--muted); font-size:.9rem; margin:.35rem 0 0; }
  ul { margin:.4rem 0 0; padding-left:1.15rem; }
  li { margin:.15rem 0; }
  a { color:var(--accent); }
  .empty { color:var(--muted); font-size:.9rem; }
  .steps { display:flex; gap:.4rem; flex-wrap:wrap; margin:.6rem 0 0; }
  .step { font-size:.75rem; padding:.2rem .55rem; border-radius:6px;
          border:1px solid var(--line); color:var(--muted); }
  .step.done { color:var(--ok); border-color:var(--ok); }
  .step.now { color:var(--accent); border-color:var(--accent);
              background:var(--accent-soft); font-weight:600; }
  .fail { color:var(--down); font-size:.85rem; margin:.25rem 0 0;
          white-space:pre-wrap; }
  code { font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size:.85em; }
</style>
</head>
<body>
<main>
  <h1>Wiz</h1>
  <p class="sub">Tell me what to build. I will plan it, build it, check it, and
     show you the result.</p>

  <form class="ask" id="ask" autocomplete="off">
    <input id="text" placeholder="Build something&hellip;" aria-label="What to build">
    <button id="go" type="submit">Build it</button>
  </form>
  <p class="hint" id="hint">&nbsp;</p>

  <div id="status"></div>

  <h2>In progress</h2>
  <div id="active"><p class="empty">Nothing in progress.</p></div>

  <h2>Ready</h2>
  <div id="ready"><p class="empty">Nothing waiting to ship.</p></div>

  <h2>Needs you</h2>
  <div id="blocked"><p class="empty">Nothing needs you.</p></div>

  <h2>Recently</h2>
  <div id="memory"><p class="empty">Nothing yet.</p></div>
</main>

<script>
const STEPS = ["UNDERSTANDING","PLANNING","BUILDING","TESTING","PREVIEWING",
               "VERIFYING","READY"];
const RISK = {HIGH:"bad", MEDIUM:"warn", LOW:"ok"};

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
async function api(path, options) {
  const res = await fetch("/api/wiz" + path, options);
  if (!res.ok && res.status !== 200) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

function steps(state) {
  const at = STEPS.indexOf(state);
  return STEPS.map((s, i) => {
    const cls = at < 0 ? "" : (i < at ? "done" : (i === at ? "now" : ""));
    return `<span class="step ${cls}">${esc(s.toLowerCase())}</span>`;
  }).join("");
}

function featureCard(f, opts) {
  opts = opts || {};
  const risk = RISK[f.risk] || "";
  const busy = ["BUILDING","TESTING","PREVIEWING","VERIFYING","PLANNING",
                "UNDERSTANDING"].includes(f.state);
  const card = el(`
    <div class="card" data-id="${esc(f.id)}">
      <div class="row">
        <h3>${esc(f.title)}</h3>
        <span class="spacer"></span>
        <span class="pill ${risk}">${esc(f.risk)} risk</span>
        <span class="pill ${busy ? "busy" : ""}">${esc(f.state)}</span>
      </div>
      <div class="steps">${steps(f.state)}</div>
      <div class="detail"></div>
    </div>`);

  const detail = card.querySelector(".detail");
  if (f.attempts > 1) {
    detail.append(el(`<p class="said">Attempt ${f.attempts}.</p>`));
  }
  if (f.preview_url) {
    detail.append(el(`<p class="said">Preview:
      <a href="${esc(f.preview_url)}" target="_blank" rel="noopener"
      >${esc(f.preview_url)}</a></p>`));
  }
  if (f.pr_url) {
    detail.append(el(`<p class="said">Pull request:
      <a href="${esc(f.pr_url)}" target="_blank" rel="noopener"
      >${esc(f.pr_url)}</a></p>`));
  }

  const actions = el('<div class="row" style="margin-top:.7rem"></div>');
  if (opts.approve) {
    const b = el('<button type="button">Approve and build</button>');
    b.onclick = () => act(`/features/${f.id}/approve`, b);
    actions.append(b);
  }
  if (opts.retry) {
    const b = el('<button type="button" class="quiet">Try again</button>');
    b.onclick = () => act(`/features/${f.id}/build`, b);
    actions.append(b);
  }
  if (opts.ship) {
    const b = el('<button type="button">Ship it</button>');
    b.onclick = () => act(`/features/${f.id}/ship`, b);
    actions.append(b);
  }
  if (busy) {
    const b = el('<button type="button" class="quiet">Stop</button>');
    b.onclick = () => act(`/features/${f.id}/cancel`, b);
    actions.append(b);
  }
  if (actions.children.length) detail.append(actions);
  return card;
}

function render(id, features, opts) {
  const host = document.getElementById(id);
  host.innerHTML = "";
  if (!features || !features.length) {
    host.append(el('<p class="empty">Nothing here.</p>'));
    return;
  }
  features.forEach(f => host.append(featureCard(f, opts)));
}

async function act(path, button) {
  button.disabled = true;
  try {
    const out = await api(path, {method: "POST"});
    if (out.message) say(out.message, out.started === false ? "warn" : "");
  } catch (e) {
    say(e.message, "bad");
  } finally {
    button.disabled = false;
    refresh();
  }
}

function say(message, kind) {
  const hint = document.getElementById("hint");
  hint.textContent = message || "\\u00a0";
  hint.style.color = kind === "bad" ? "var(--down)"
                   : kind === "warn" ? "var(--warn)" : "var(--muted)";
}

async function refresh() {
  try {
    const status = await api("/status");
    const host = document.getElementById("status");
    host.innerHTML = "";
    if (!status.configured || !status.can_build) {
      const missing = (status.checks || []).filter(c => !c.ok)
        .map(c => `<li>${esc(c.name)}: ${esc(c.detail || "missing")}</li>`).join("");
      host.append(el(`<div class="banner warn">
        <strong>I cannot build anything yet.</strong>
        <ul>${missing}</ul></div>`));
    } else if (!status.can_verify) {
      host.append(el(`<div class="banner warn">I can build, but I cannot check a
        preview in a browser, so nothing will reach "ready" on its own.</div>`));
    }

    const listed = await api("/features");
    if (listed.ok) {
      render("active", listed.result.building, {});
      render("ready", listed.result.ready, {ship: true});
      render("blocked", listed.result.waiting_for_you, {retry: true, approve: true});
    }

    const memory = await api("/memory?limit=8");
    const host2 = document.getElementById("memory");
    host2.innerHTML = "";
    const entries = (memory.ok && memory.result.entries) || [];
    if (!entries.length) {
      host2.append(el('<p class="empty">Nothing yet.</p>'));
    } else {
      entries.forEach(e => host2.append(el(
        `<div class="card"><div class="row"><h3>${esc(e.title)}</h3>
         <span class="spacer"></span>
         <span class="pill">${esc(e.kind)}</span></div>
         <p class="said">${esc((e.at || "").slice(0, 10))}</p></div>`)));
    }
  } catch (e) {
    say(e.message, "bad");
  }
}

document.getElementById("ask").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("text");
  const text = input.value.trim();
  if (!text) return;
  const go = document.getElementById("go");
  go.disabled = true;
  say("Understanding\\u2026");
  try {
    const out = await api("/features", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text}),
    });
    if (!out.ok) { say(out.message, "warn"); return; }
    input.value = "";
    say(out.result.say + (out.started ? "" : " " + (out.message || "")),
        out.started ? "" : "warn");
  } catch (e) {
    say(e.message, "bad");
  } finally {
    go.disabled = false;
    refresh();
  }
});

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@router.get("/wiz", response_class=HTMLResponse, include_in_schema=False)
def wiz_page() -> HTMLResponse:
    """The Control Center's build page."""
    return HTMLResponse(content=_PAGE)
