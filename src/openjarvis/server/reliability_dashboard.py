"""The JARVIS dashboard page.

A single self-contained HTML page served by the API rather than a React route.

The reasoning: the React SPA under ``frontend/`` needs a build step
(``npm run build``) before a new page is visible, which would make the
dashboard invisible to anyone running JARVIS from a source checkout without a
Node toolchain. A server-rendered page works the moment ``jarvis serve`` starts.
It reads the same read-only endpoints a React page would, so moving it into the
SPA later is a straight port with no API changes.
"""

from __future__ import annotations

try:
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
except ImportError:  # pragma: no cover - server extra not installed
    raise ImportError("fastapi is required for the reliability dashboard")

router = APIRouter(tags=["reliability"])

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS</title>
<style>
  :root {
    --bg: #ffffff; --fg: #14161a; --muted: #6b7280; --line: #e5e7eb;
    --card: #f9fafb; --ok: #15803d; --warn: #b45309; --down: #b91c1c;
    --accent: #1d4ed8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --fg: #e6e8eb; --muted: #9aa1ab; --line: #262b33;
      --card: #161a21; --ok: #4ade80; --warn: #fbbf24; --down: #f87171;
      --accent: #60a5fa;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem 1.25rem; background: var(--bg); color: var(--fg);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width: 1000px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: .02em; }
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .09em;
       color: var(--muted); margin: 2rem 0 .75rem; font-weight: 600; }
  .sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
  .tiles { display: grid; gap: .75rem;
           grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
  .tile { background: var(--card); border: 1px solid var(--line);
          border-radius: 10px; padding: .9rem 1rem; }
  .tile .label { font-size: .75rem; text-transform: uppercase;
                 letter-spacing: .07em; color: var(--muted); }
  .tile .value { font-size: 1.05rem; font-weight: 600; margin-top: .3rem;
                 display: flex; align-items: center; gap: .45rem; }
  .dot { width: .6rem; height: .6rem; border-radius: 50%; flex: none; }
  .healthy .dot { background: var(--ok); } .healthy .value { color: var(--ok); }
  .degraded .dot { background: var(--warn); } .degraded .value { color: var(--warn); }
  .down .dot { background: var(--down); } .down .value { color: var(--down); }
  .disabled .dot { background: var(--muted); } .disabled .value { color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  th { text-align: left; font-size: .72rem; text-transform: uppercase;
       letter-spacing: .07em; color: var(--muted); font-weight: 600;
       padding: .5rem .6rem; border-bottom: 1px solid var(--line); }
  td { padding: .55rem .6rem; border-bottom: 1px solid var(--line);
       vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .sev { font-weight: 600; font-size: .78rem; letter-spacing: .03em; }
  .sev-CRITICAL { color: var(--down); } .sev-HIGH { color: var(--down); }
  .sev-MEDIUM { color: var(--warn); } .sev-LOW { color: var(--muted); }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size: .85em; }
  .empty { color: var(--muted); padding: 1rem 0; }
  .scroll { overflow-x: auto; }
  footer { margin-top: 2.5rem; color: var(--muted); font-size: .8rem;
           border-top: 1px solid var(--line); padding-top: 1rem; }
  .yes { color: var(--ok); font-weight: 600; }
  .no { color: var(--down); font-weight: 600; }
</style>
</head>
<body>
<main>
  <h1>JARVIS</h1>
  <p class="sub" id="sub">Loading…</p>

  <h2>System status</h2>
  <div class="tiles" id="tiles"></div>

  <h2>Active incidents</h2>
  <div class="scroll"><div id="incidents"></div></div>

  <h2>Recent repairs</h2>
  <div class="scroll"><div id="repairs"></div></div>

  <h2>Probes</h2>
  <div class="scroll"><div id="probes"></div></div>

  <footer id="footer"></footer>
</main>
<script>
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

function table(headers, rows, emptyMessage) {
  if (!rows.length) return `<p class="empty">${esc(emptyMessage)}</p>`;
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.map((cells) => `<tr>${cells.join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function render() {
  const health = await get("/api/reliability/health");
  document.getElementById("sub").textContent = health.enabled
    ? `Monitoring ${health.site || "(no site configured)"} · ${health.environment}`
    : "Monitoring is disabled.";

  const surfaces = health.surfaces || {};
  document.getElementById("tiles").innerHTML =
    Object.entries(surfaces).map(([name, status]) => `
      <div class="tile ${esc(status)}">
        <div class="label">${esc(name)}</div>
        <div class="value"><span class="dot"></span>${esc(status)}</div>
      </div>`).join("") + `
      <div class="tile">
        <div class="label">Open incidents</div>
        <div class="value">${health.open_incidents}</div>
      </div>
      <div class="tile">
        <div class="label">Deploy mode</div>
        <div class="value">${esc(health.policy.deploy_mode)}</div>
      </div>`;

  const inc = await get("/api/reliability/incidents?open_only=true&limit=50");
  document.getElementById("incidents").innerHTML = table(
    ["ID", "Severity", "State", "Component", "Title", "Seen"],
    inc.incidents.map((i) => [
      `<td><code>${esc(i.id)}</code></td>`,
      `<td class="sev sev-${esc(i.severity)}">${esc(i.severity)}</td>`,
      `<td>${esc(i.state)}</td>`,
      `<td>${esc(i.component)}</td>`,
      `<td>${esc(i.title)}</td>`,
      `<td>${esc(i.occurrences)}</td>`,
    ]),
    "No active incidents."
  );

  const rep = await get("/api/reliability/repairs?limit=15");
  document.getElementById("repairs").innerHTML = table(
    ["Incident", "Attempt", "Files", "Tests", "Verified", "Outcome"],
    rep.repairs.map((r) => [
      `<td><code>${esc(r.incident_id)}</code></td>`,
      `<td>${esc(r.attempt)}</td>`,
      `<td>${esc((r.changed_files || []).length)}</td>`,
      `<td>${r.tests_passed === null ? "—" : esc(r.tests_passed)}</td>`,
      `<td class="${r.verified ? "yes" : "no"}">${r.verified ? "yes" : "no"}</td>`,
      `<td>${esc(r.outcome)}</td>`,
    ]),
    "No repair attempts yet."
  );

  const pr = await get("/api/reliability/probes");
  document.getElementById("probes").innerHTML = table(
    ["ID", "Component", "Severity", "Runner", "Schedule", "Enabled"],
    pr.probes.map((p) => [
      `<td><code>${esc(p.id)}</code></td>`,
      `<td>${esc(p.component)}</td>`,
      `<td class="sev sev-${esc(p.severity)}">${esc(p.severity)}</td>`,
      `<td>${esc(p.runner)}</td>`,
      `<td><code>${esc(p.schedule)}</code></td>`,
      `<td class="${p.enabled ? "yes" : "no"}">${p.enabled ? "yes" : "no"}</td>`,
    ]),
    "No probe specs configured."
  );

  document.getElementById("footer").textContent =
    `Audit chain ${health.audit_chain_intact ? "intact" : "BROKEN"} · ` +
    `automated repair ${health.policy.repair_enabled ? "enabled" : "disabled"} · ` +
    `refreshed ${new Date().toLocaleTimeString()}`;
}

render().catch((err) => {
  document.getElementById("sub").textContent = "Could not load: " + err.message;
});
setInterval(() => render().catch(() => {}), 30000);
</script>
</body>
</html>
"""


@router.get("/reliability", response_class=HTMLResponse)
def reliability_dashboard() -> HTMLResponse:
    """Serve the JARVIS dashboard."""
    return HTMLResponse(content=_PAGE)
