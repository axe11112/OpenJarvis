/* JARVIS Control Center — client.
 *
 * Renders whatever /api/snapshot says and nothing else. There is no local
 * state machine, no derived status and no cached verdict: if the server has
 * not concluded something, the screen does not claim it. Every value shown
 * here came out of the reliability system a few seconds ago.
 *
 * All text is inserted through textContent. Incident titles and evidence are
 * captured from the monitored application — untrusted by construction — and
 * they are redacted server-side and never parsed as HTML here.
 */
(function () {
  "use strict";

  var ROOT = document.documentElement;
  var TOKEN = ROOT.dataset.controlToken || "";
  var CAN_CONTROL = ROOT.dataset.watcherControl === "true";
  var POLL_MS = 3000;

  var el = function (id) { return document.getElementById(id); };
  var last = null;
  var openIncidentId = null;

  /* ------------------------------------------------------------ helpers */

  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = String(text); }
    return n;
  }

  function clear(target) {
    while (target.firstChild) { target.removeChild(target.firstChild); }
  }

  function pretty(value) {
    return String(value || "").replace(/_/g, " ");
  }

  function ago(iso) {
    if (!iso) { return "never"; }
    var then = Date.parse(iso);
    if (isNaN(then)) { return "unknown"; }
    var secs = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (secs < 60) { return secs + "s ago"; }
    if (secs < 3600) { return Math.round(secs / 60) + "m ago"; }
    if (secs < 86400) { return Math.round(secs / 3600) + "h ago"; }
    return Math.round(secs / 86400) + "d ago";
  }

  function until(iso) {
    if (!iso) { return "—"; }
    var then = Date.parse(iso);
    if (isNaN(then)) { return "—"; }
    var secs = Math.max(0, Math.round((then - Date.now()) / 1000));
    if (secs === 0) { return "due now"; }
    if (secs < 60) { return "in " + secs + "s"; }
    var mins = Math.floor(secs / 60);
    return "in " + mins + "m " + (secs % 60) + "s";
  }

  function localTime(iso) {
    if (!iso) { return ""; }
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "" : d.toLocaleString();
  }

  function getJSON(path) {
    return fetch(path, { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      });
  }

  function postAction(path) {
    return fetch(path, {
      method: "POST",
      headers: { "X-JARVIS-Control": TOKEN, "Accept": "application/json" }
    }).then(function (r) { return r.json(); });
  }

  /* ------------------------------------------------------------ header */

  function renderHeader(s) {
    el("wiz").dataset.mood = (s.wiz && s.wiz.mood) || "thinking";
    el("wizHeadline").textContent = (s.wiz && s.wiz.headline) || "";
    el("wizDetail").textContent = (s.wiz && s.wiz.detail) || "";

    var mode = el("modeText");
    var modeEl = mode.parentElement;
    if (!s.monitoring_enabled) {
      mode.textContent = "MONITORING DISABLED";
      modeEl.classList.add("off");
    } else if (s.safety && s.safety.emergency_stop_engaged) {
      mode.textContent = "EMERGENCY STOP";
      modeEl.classList.add("off");
    } else {
      mode.textContent = "MONITORING";
      modeEl.classList.remove("off");
    }

    var overall = el("overall");
    overall.dataset.state = s.overall;
    el("overallValue").textContent = pretty(s.overall);
    el("overallTarget").textContent =
      s.target.environment + " · " + (s.target.repository || s.target.url || "");

    el("targetName").textContent = s.target.name;
    el("targetUrl").textContent = s.target.url || "no site URL configured";

    el("lastCycle").textContent = s.cycle.running
      ? "checking now…"
      : ago(s.cycle.last_at);
    el("lastCycleAbs").textContent = localTime(s.cycle.last_at);
    el("nextCycle").textContent = until(s.cycle.next_at);
    el("cycleInterval").textContent =
      "every " + Math.round(s.cycle.interval_seconds) + "s · probe verification "
      + s.cycle.probe_verification;

    el("generatedAt").textContent = "updated " + localTime(s.generated_at);
  }

  /* ------------------------------------------------------------ watcher */

  function renderWatcher(s) {
    var w = s.watcher || {};
    var box = el("watcherBox");
    box.dataset.status = w.status || "OFFLINE";
    el("watcherStatus").textContent = pretty(w.status || "UNKNOWN");
    el("watcherDetail").textContent = w.detail || "";

    var actions = el("watcherActions");
    var offerable = CAN_CONTROL && w.supervisor_supported && w.service_installed;
    actions.hidden = !offerable;
    if (offerable) {
      // A start is refused while the emergency stop is engaged, and the button
      // says so rather than failing after the click.
      var stopped = w.status === "STOPPED_BY_OPERATOR";
      el("btnStart").disabled = stopped || w.status === "ONLINE";
      el("btnRestart").disabled = stopped;
      el("btnStart").title = stopped ? w.start_blocked_reason : "";
      el("btnRestart").title = stopped ? w.start_blocked_reason : "";
    }
  }

  /* ------------------------------------------------------------ cards */

  function renderCards(s) {
    var host = el("cards");
    clear(host);
    s.cards.forEach(function (c) {
      var card = node("article", "card");
      card.dataset.state = c.state;

      var head = node("div", "card-head");
      head.appendChild(node("h3", null, c.title));
      head.appendChild(node("span", "state", pretty(c.state)));
      card.appendChild(head);

      if (c.summary) { card.appendChild(node("p", "summary", c.summary)); }

      if (c.facts && c.facts.length) {
        var facts = node("div", "facts");
        c.facts.slice(0, 6).forEach(function (f) {
          var row = node("div");
          row.appendChild(node("span", null, f.label));
          row.appendChild(node("b", null, f.value));
          facts.appendChild(row);
        });
        card.appendChild(facts);
      }

      if (c.blind_spots && c.blind_spots.length) {
        var blind = node("div", "blind");
        blind.appendChild(node("span", null,
          c.blind_spots.length + " not verified — blind spots, not passes"));
        var list = node("ul");
        c.blind_spots.slice(0, 4).forEach(function (b) {
          list.appendChild(node("li", null, b));
        });
        blind.appendChild(list);
        card.appendChild(blind);
      }
      host.appendChild(card);
    });
  }

  /* ------------------------------------------------------------ incidents */

  function renderIncidents(s) {
    var host = el("incidents");
    clear(host);
    el("incidentCount").textContent =
      s.open_incident_count + " open · " + s.resolved_incident_count + " resolved";

    if (!s.incidents.length) {
      host.appendChild(node("div", "empty",
        "No incidents on record. JARVIS has opened nothing."));
      return;
    }
    if (!s.open_incident_count) {
      host.appendChild(node("div", "empty",
        "No open incidents. Everything below has been resolved."));
    }

    s.incidents.forEach(function (i) {
      var row = node("button", "incident" + (i.is_open ? "" : " resolved"));
      row.type = "button";
      row.appendChild(node("span", "sev " + i.severity, i.severity));
      row.appendChild(node("span",
        "istate " + (i.is_open ? "open" : "RESOLVED"), pretty(i.state)));

      var title = node("div", "ititle");
      title.appendChild(node("b", null, i.title));
      title.appendChild(node("span", null,
        i.id + " · " + i.component + (i.probe_id ? " · " + i.probe_id : "")
        + " · detected " + ago(i.detected_at)));
      row.appendChild(title);

      var meta = node("div", "imeta");
      var seen = node("span");
      seen.appendChild(node("b", null, i.occurrences));
      seen.appendChild(document.createTextNode(
        i.occurrences === 1 ? " occurrence" : " occurrences"));
      meta.appendChild(seen);
      meta.appendChild(node("span", null,
        i.attempts + (i.attempts === 1 ? " repair attempt" : " repair attempts")));
      if (i.flapping) { meta.appendChild(node("span", null, "flapping")); }
      row.appendChild(meta);

      row.addEventListener("click", function () { openIncident(i.id); });
      host.appendChild(row);
    });
  }

  /* ------------------------------------------------------------ probes */

  var PROBE_CLASS = {
    PASS: "pass", FAIL: "fail",
    NOT_VERIFIED: "notverified", KNOWN_NOISE: "noise"
  };

  function renderProbes(s) {
    var body = el("probes");
    clear(body);
    el("probeCount").textContent = s.probes.length + " configured";

    if (!s.probes.length) {
      var empty = node("tr");
      var cell = node("td", null, "No probe specs configured.");
      cell.colSpan = 7;
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }

    s.probes.forEach(function (p) {
      var tr = node("tr");

      var status = node("td");
      status.appendChild(node("span",
        "chip " + (PROBE_CLASS[p.status] || "notverified"), pretty(p.status)));
      tr.appendChild(status);

      var name = node("td", "pid" + (p.enabled ? "" : " disabled"));
      name.appendChild(node("b", null, p.name));
      name.appendChild(node("span", null, p.reason || p.id));
      tr.appendChild(name);

      tr.appendChild(node("td", null, p.component));
      tr.appendChild(node("td", "num", p.severity));
      tr.appendChild(node("td", "num", p.schedule));
      tr.appendChild(node("td", "num", p.last_run ? ago(p.last_run) : "never"));
      tr.appendChild(node("td", "num",
        p.duration_seconds === null || p.duration_seconds === undefined
          ? "—" : p.duration_seconds.toFixed(2) + "s"));
      body.appendChild(tr);
    });
  }

  /* ------------------------------------------------------------ safety */

  function renderSafety(s) {
    var host = el("safety");
    clear(host);
    s.safety.rows.forEach(function (r) {
      var row = node("div", "srow" + (r.dangerous ? " danger" : ""));
      row.appendChild(node("span", "k", r.label));
      row.appendChild(node("span", "v", r.value));
      if (r.detail) { row.appendChild(node("span", "d", r.detail)); }
      host.appendChild(row);
    });

    var note = el("auditNote");
    note.classList.remove("bad");
    if (s.audit_chain_intact === true) {
      note.textContent = "Audit chain intact — every incident transition is hash-chained.";
    } else if (s.audit_chain_intact === false) {
      note.textContent = "AUDIT CHAIN BROKEN — the transition log has been altered.";
      note.classList.add("bad");
    } else {
      note.textContent = "Audit chain could not be verified.";
    }
  }

  /* ------------------------------------------------------------ drawer */

  function openIncident(id) {
    openIncidentId = id;
    el("drawerBackdrop").hidden = false;
    el("drawer").hidden = false;
    getJSON("/api/incidents/" + encodeURIComponent(id))
      .then(renderDrawer)
      .catch(function (e) {
        var body = el("drawerBody");
        clear(body);
        body.appendChild(node("div", "notice bad", "Could not load " + id + ": " + e.message));
      });
  }

  function closeDrawer() {
    openIncidentId = null;
    el("drawerBackdrop").hidden = true;
    el("drawer").hidden = true;
  }

  function kv(dl, key, value) {
    if (value === undefined || value === null || value === "") { return; }
    dl.appendChild(node("dt", null, key));
    dl.appendChild(node("dd", null, value));
  }

  function renderDrawer(d) {
    var body = el("drawerBody");
    clear(body);

    body.appendChild(node("h3", null, d.title));
    body.appendChild(node("p", "dsub",
      d.id + " · " + d.severity + " · " + pretty(d.state)
      + " · " + d.component + " · " + d.environment));

    if (d.summary) { body.appendChild(node("p", null, d.summary)); }

    body.appendChild(node("h4", null, "Facts"));
    var facts = node("dl", "kv");
    kv(facts, "Detected", localTime(d.created_at));
    kv(facts, "Last seen", localTime(d.last_seen_at));
    kv(facts, "Occurrences", d.occurrences);
    kv(facts, "Source", d.source);
    kv(facts, "Probe", d.probe_id);
    kv(facts, "Fingerprint", d.fingerprint);
    kv(facts, "Repair attempts", (d.attempts || []).length);
    Object.keys(d.metadata || {}).forEach(function (k) {
      kv(facts, pretty(k), d.metadata[k]);
    });
    body.appendChild(facts);

    if (d.repro_steps && d.repro_steps.length) {
      body.appendChild(node("h4", null, "Reproduction"));
      var steps = node("ol");
      d.repro_steps.forEach(function (s) { steps.appendChild(node("li", null, s)); });
      body.appendChild(steps);
    }

    body.appendChild(node("h4", null, "Evidence (" + (d.evidence || []).length + ")"));
    if (!(d.evidence || []).length) {
      body.appendChild(node("p", "dsub", "No evidence recorded."));
    }
    (d.evidence || []).forEach(function (e) {
      var card = node("div", "ev");
      var head = node("div", "evh");
      head.appendChild(node("span", null, pretty(e.kind)));
      head.appendChild(node("span", null, localTime(e.created_at)));
      if (e.source) { head.appendChild(node("span", null, e.source)); }
      if (e.trust === "external") {
        head.appendChild(node("span", "ext", "external · untrusted · redacted"));
      }
      if (e.has_artifact) { head.appendChild(node("span", null, "artifact on disk")); }
      card.appendChild(head);
      if (e.summary) { card.appendChild(node("p", null, e.summary)); }
      if (e.content) { card.appendChild(node("pre", null, e.content)); }
      body.appendChild(card);
    });

    if ((d.attempts || []).length) {
      body.appendChild(node("h4", null, "Repair attempts"));
      d.attempts.forEach(function (a) {
        var card = node("div", "ev");
        var head = node("div", "evh");
        head.appendChild(node("span", null, "attempt " + a.number));
        head.appendChild(node("span", null, localTime(a.started_at)));
        head.appendChild(node("span", null, a.outcome || "no outcome recorded"));
        card.appendChild(head);
        var dl = node("dl", "kv");
        kv(dl, "Branch", a.branch);
        kv(dl, "Base commit", a.base_commit);
        kv(dl, "Changed files", (a.changed_files || []).join(", "));
        kv(dl, "Diff", a.diff_stat);
        kv(dl, "Tests passed", a.tests_passed === null ? "not run" : String(a.tests_passed));
        kv(dl, "Verified", a.verification ? String(!!a.verification.passed) : "not verified");
        card.appendChild(dl);
        if (a.claim) { card.appendChild(node("pre", null, a.claim)); }
        body.appendChild(card);
      });
    } else {
      body.appendChild(node("h4", null, "Repair attempts"));
      body.appendChild(node("p", "dsub", "None. JARVIS has not attempted a repair."));
    }

    body.appendChild(node("h4", null, "History"));
    var tl = node("div", "tl");
    var transitions = (d.audit && d.audit.transitions) || [];
    if (!transitions.length) {
      tl.appendChild(node("p", "dsub", "No transitions recorded."));
    }
    transitions.forEach(function (t) {
      var item = node("div", "tl-item");
      item.appendChild(node("span", "when", localTime(t.at)));
      item.appendChild(document.createTextNode(
        pretty(t.from_state) + " → " + pretty(t.to_state)));
      if (t.reason) { item.appendChild(node("div", null, t.reason)); }
      item.appendChild(node("span", "who", "by " + (t.actor || "jarvis")));
      tl.appendChild(item);
    });
    body.appendChild(tl);

    body.appendChild(node("h4", null, "Audit"));
    var audit = node("dl", "kv");
    kv(audit, "Recorded transitions", d.audit.recorded_transitions);
    kv(audit, "Hash chain",
      d.audit.chain_intact === true ? "intact"
        : d.audit.chain_intact === false ? "BROKEN" : "not verified");
    kv(audit, "Terminal", String(!!d.is_terminal));
    body.appendChild(audit);

    body.appendChild(node("div", "notice",
      "Read-only view. Closing, repairing and re-opening incidents are audited "
      + "actions and stay on the command line."));
  }

  /* ------------------------------------------------------------ actions */

  function wireActions() {
    function act(path, button) {
      var buttons = [el("btnStart"), el("btnRestart")];
      buttons.forEach(function (b) { b.disabled = true; });
      var out = el("watcherResult");
      out.className = "s action-result";
      out.textContent = "asking launchd…";
      postAction(path)
        .then(function (r) {
          out.textContent = r.message || (r.ok ? "requested" : "refused");
          out.className = "s action-result " + (r.ok ? "ok" : "bad");
          return refresh();
        })
        .catch(function (e) {
          out.textContent = "request failed: " + e.message;
          out.className = "s action-result bad";
        })
        .then(function () {
          buttons.forEach(function (b) { b.disabled = false; });
        });
    }
    el("btnStart").addEventListener("click", function () {
      act("/api/watcher/start");
    });
    el("btnRestart").addEventListener("click", function () {
      act("/api/watcher/restart");
    });
    el("drawerClose").addEventListener("click", closeDrawer);
    el("drawerBackdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && openIncidentId) { closeDrawer(); }
    });
  }

  /* ------------------------------------------------------------ loop */

  function render(s) {
    last = s;
    renderHeader(s);
    renderWatcher(s);
    renderCards(s);
    renderIncidents(s);
    renderProbes(s);
    renderSafety(s);
    el("boot").hidden = true;
    el("app").hidden = false;
  }

  function refresh() {
    return getJSON("/api/snapshot")
      .then(render)
      .catch(function (e) {
        if (!last) {
          el("boot").textContent = "Cannot reach the Control Center: " + e.message;
        }
      });
  }

  wireActions();
  refresh();
  setInterval(refresh, POLL_MS);
  // Re-tick the countdowns between polls so "next cycle" does not sit still.
  setInterval(function () { if (last) { renderHeader(last); } }, 1000);
})();
