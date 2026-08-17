/* Sir Voice — the browser half of a call.
 *
 * Three things here are worth knowing before changing anything.
 *
 * 1. Recording goes through MediaRecorder, and the Mac converts whatever
 *    container comes back. The previous version encoded 16 kHz WAV here from
 *    raw Web Audio samples, to spare the Mac a decoder; on a real iPhone,
 *    inside an installed PWA, that graph delivered empty buffers and every
 *    utterance silently uploaded nothing. One decoder on the Mac is worth more
 *    than clever DSP that only works in the browsers you tested.
 *
 * 2. A turn is push-to-talk. The operator is often outdoors, in a room with a
 *    television, or holding the phone loosely; a system that decides for itself
 *    when speech began is a system that transcribes the television.
 *
 * 3. Nothing fails silently, ever. Every path that ends without an upload says
 *    so on screen — that was the whole difference between "Sir is broken" and
 *    "hold the button a little longer".
 */
(() => {
  "use strict";

  const TOKEN = document.body.dataset.controlToken;
  const MAX_UTTERANCE_MS = 20000;   // hard stop, so a stuck button cannot run on

  const el = (id) => document.getElementById(id);
  const ringing = el("ringing");
  const call = el("call");
  const state = el("state");
  const transcript = el("transcript");

  let sessionId = null;
  let stream = null;
  let recorder = null;
  let chunks = [];
  let recording = false;
  let startedAt = 0;
  let maxTimer = null;

  /* ---------------------------------------------------------------- api */

  async function api(path, { method = "GET", body = null, type = null } = {}) {
    const headers = { "X-JARVIS-Control": TOKEN };
    if (type) headers["Content-Type"] = type;
    const response = await fetch(path, { method, headers, body, cache: "no-store" });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty body */ }
    return { status: response.status, payload };
  }

  function say(who, text, cls) {
    if (!text) return;
    const line = document.createElement("p");
    line.className = `line line--${who}${cls ? " " + cls : ""}`;
    line.textContent = text;
    transcript.appendChild(line);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function play(base64) {
    if (!base64) return;
    const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "audio/aiff" }));
    const audio = new Audio(url);
    // Revoked as soon as it has played: no synthesised speech is left addressable.
    audio.onended = audio.onerror = () => URL.revokeObjectURL(url);
    audio.play().catch(() => URL.revokeObjectURL(url));
  }

  /* ------------------------------------------------------------ capture
   *
   * MediaRecorder, not Web Audio.
   *
   * The first version encoded WAV here from raw ScriptProcessor samples, to
   * spare the Mac a decoder. On a real iPhone, inside an installed PWA, that
   * graph delivers empty buffers: every utterance produced zero chunks, the
   * upload was skipped, and the call looked alive while hearing nothing.
   *
   * MediaRecorder is the one recording path every browser genuinely supports.
   * It hands back its own container — audio/mp4 on iOS, audio/webm on Chrome —
   * and the Mac normalises it. One decoder there beats per-browser DSP here.
   */

  function pickMimeType() {
    // Ordered by what decodes most reliably on the Mac. Safari ignores the
    // option entirely and returns mp4 regardless, which is fine — the server
    // sniffs the real container from the bytes rather than trusting a label.
    const candidates = [
      "audio/mp4",
      "audio/mp4;codecs=mp4a.40.2",
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
    ];
    if (typeof MediaRecorder === "undefined") return "";
    for (const type of candidates) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(type)) return type;
    }
    return "";
  }

  async function ensureMic() {
    if (stream) return true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      fail("This page cannot use the microphone. Open Sir over https on your Tailscale name.");
      return false;
    }
    if (typeof MediaRecorder === "undefined") {
      fail("This browser cannot record audio. Use Safari on iOS, or Chrome.");
      return false;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
    } catch (err) {
      // Distinguish "said no" from "no microphone here", because the fix is
      // different and the operator is holding a phone in the dark.
      fail(
        err && err.name === "NotAllowedError"
          ? "I need permission to use the microphone. Allow it in Settings and try again."
          : "I could not open the microphone: " + ((err && err.name) || "unknown error")
      );
      return false;
    }
    return true;
  }

  function fail(message) {
    state.textContent = message;
    say("sir", message, "line--note");
  }

  async function startTurn() {
    if (recording || !sessionId) return;
    if (!(await ensureMic())) return;

    const mimeType = pickMimeType();
    try {
      recorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
    } catch (err) {
      fail("This browser refused to start recording.");
      return;
    }

    chunks = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };
    recorder.onerror = () => fail("The recording stopped unexpectedly.");
    recorder.onstop = uploadTurn;

    // A timeslice means data arrives during the recording rather than only at
    // the end, so a tab suspended mid-utterance still yields what it captured.
    recorder.start(250);
    recording = true;
    startedAt = performance.now();
    el("talk-label").textContent = "listening…";
    document.body.classList.add("listening");

    clearTimeout(maxTimer);
    maxTimer = setTimeout(() => { if (recording) stopTurn(); }, MAX_UTTERANCE_MS);
  }

  function stopTurn() {
    if (!recording) return;
    recording = false;
    clearTimeout(maxTimer);
    document.body.classList.remove("listening");
    el("talk-label").textContent = "Hold to talk";
    const heldFor = performance.now() - startedAt;
    try {
      recorder.stop();
    } catch (err) {
      fail("I could not finish the recording.");
      return;
    }
    if (heldFor < 400) {
      // Almost certainly a tap rather than a hold. Say so: silence here is
      // what made this feel broken rather than merely fiddly.
      state.textContent = "hold the button while you speak";
    }
  }

  async function uploadTurn() {
    const captured = chunks;
    chunks = [];
    const type = (recorder && recorder.mimeType) || "application/octet-stream";
    if (!captured.length) {
      // Never silent. This is exactly the case that made every iPhone
      // utterance vanish without a word.
      fail("I did not get any audio from the microphone. Try holding the button longer.");
      return;
    }

    const blob = new Blob(captured, { type });
    if (blob.size < 2000) {
      fail("That was too short for me to hear. Hold the button while you speak.");
      return;
    }

    state.textContent = "thinking…";
    const { status, payload } = await api(
      `/api/voice/utterance?id=${encodeURIComponent(sessionId)}`,
      { method: "POST", body: blob, type }
    );
    await handleTurn(status, payload);
  }

  async function handleTurn(status, payload) {
    if (status === 409 && payload.reconnect) {
      say("sir", "The call had ended, so I picked up again.", "line--note");
      await answer();
      return;
    }
    if (status !== 200) {
      state.textContent = payload.error || "something went wrong";
      return;
    }
    if (payload.heard) say("you", payload.heard);
    say("sir", payload.speech, payload.risk === "CONFIRM" ? "line--confirm" : "");
    play(payload.audio);
    state.textContent = payload.ended ? "call ended" : "listening when you are";
    if (payload.confirmation_id) refreshPending();
    if (payload.ended) endCall(false);
  }

  /* --------------------------------------------------------- call flow */

  async function answer() {
    state.textContent = "connecting…";
    ringing.hidden = true;
    call.hidden = false;
    const { status, payload } = await api("/api/voice/answer", { method: "POST" });
    if (status !== 200) {
      state.textContent = payload.error || "Sir could not pick up";
      return;
    }
    sessionId = payload.session;
    say("sir", payload.speech);
    play(payload.audio);
    state.textContent = "listening when you are";
    // Ask once, up front, so the first press of the talk button is not also a
    // permission prompt the operator has to read during an outage.
    ensureMic();
    enablePush(swRegistration).catch(() => {});
  }

  async function endCall(tellServer = true) {
    if (tellServer && sessionId) {
      await api(`/api/voice/hangup?id=${encodeURIComponent(sessionId)}`, { method: "POST" });
    }
    sessionId = null;
    recording = false;
    clearTimeout(maxTimer);
    // Stopping the tracks is what turns the recording indicator off. Leaving
    // them live would keep the microphone lit after the call ended, which is
    // both alarming and true.
    if (recorder && recorder.state !== "inactive") {
      try { recorder.stop(); } catch (_) { /* already stopping */ }
    }
    recorder = null;
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    state.textContent = "call ended";
  }

  async function refreshPending() {
    const { payload } = await api("/api/voice/pending");
    const list = el("pending-list");
    const items = payload.confirmations || [];
    list.innerHTML = "";
    el("pending").hidden = items.length === 0;
    for (const item of items) {
      const li = document.createElement("li");
      const text = document.createElement("span");
      text.textContent = item.description;
      li.appendChild(text);
      for (const decision of ["approve", "decline"]) {
        const button = document.createElement("button");
        button.className = `pill pill--${decision}`;
        button.textContent = decision === "approve" ? "Approve" : "Decline";
        button.onclick = async () => {
          await api("/api/voice/confirm", {
            method: "POST",
            type: "application/json",
            body: JSON.stringify({ id: item.id, decision }),
          });
          refreshPending();
        };
        li.appendChild(button);
      }
      list.appendChild(li);
    }
    // Show why Sir called, if he did.
    if (payload.call) {
      el("ring-reason").textContent = "needs your attention";
      el("ring-detail").textContent = payload.call.detail || "";
    }
  }

  /* -------------------------------------------------------------- wire */

  el("answer").addEventListener("click", answer);
  el("decline").addEventListener("click", async () => {
    ringing.hidden = true;
    // Tell the Mac, so the cooldown is honoured. A decline that only closes a
    // screen means the next watcher tick rings again in sixty seconds.
    await api("/api/voice/decline", { method: "POST" });
    endCall();
  });
  el("hangup").addEventListener("click", () => endCall());

  const talk = el("talk");
  talk.addEventListener("pointerdown", (e) => { e.preventDefault(); startTurn(); });
  talk.addEventListener("pointerup", (e) => { e.preventDefault(); stopTurn(); });
  talk.addEventListener("pointercancel", () => stopTurn());

  el("send").addEventListener("click", async () => {
    const input = el("typed");
    const text = input.value.trim();
    if (!text || !sessionId) return;
    input.value = "";
    const { status, payload } = await api(
      `/api/voice/text?id=${encodeURIComponent(sessionId)}`,
      { method: "POST", type: "application/json", body: JSON.stringify({ text }) }
    );
    await handleTurn(status, payload);
  });
  el("typed").addEventListener("keydown", (e) => {
    if (e.key === "Enter") el("send").click();
  });

  // A phone that slept mid-call comes back here. Re-checking on wake is what
  // makes "it just carried on" true rather than aspirational.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && sessionId) {
      api(`/api/voice/session?id=${encodeURIComponent(sessionId)}`).then(({ payload }) => {
        if (!payload.live) {
          say("sir", "That call had ended while your phone was asleep.", "line--note");
          sessionId = null;
          state.textContent = "call ended";
        }
      });
    }
  });

  /* ------------------------------------------------------------- push */

  async function enablePush(registration) {
    if (!("PushManager" in window) || !registration) return;
    const { payload } = await api("/api/voice/push-key");
    if (!payload.enabled || !payload.key) return;

    // Asking on load would burn the one prompt iOS gives per install on a
    // visitor who has not decided they want this yet. Ask on the first answer,
    // when they have just chosen to talk to Sir.
    if (Notification.permission === "denied") return;
    if (Notification.permission === "default") {
      const decision = await Notification.requestPermission();
      if (decision !== "granted") return;
    }

    const existing = await registration.pushManager.getSubscription();
    const subscription =
      existing ||
      (await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(payload.key),
      }));
    await api("/api/voice/subscribe", {
      method: "POST",
      type: "application/json",
      body: JSON.stringify(subscription.toJSON()),
    });
    el("footnote").textContent = "Sir can ring this phone.";
  }

  function urlBase64ToUint8Array(value) {
    const padded = (value + "=".repeat((4 - (value.length % 4)) % 4))
      .replace(/-/g, "+")
      .replace(/_/g, "/");
    const raw = atob(padded);
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  let swRegistration = null;
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker
      .register("/sw.js")
      .then((registration) => { swRegistration = registration; })
      .catch(() => {});
  }

  /* ----------------------------------------------------------- health */

  async function refreshHealth() {
    const { payload } = await api("/api/voice/health");
    const list = el("health-list");
    if (!list || !payload.parts) return;
    list.innerHTML = "";

    const row = (label, state, detail) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = label;
      const value = document.createElement("b");
      value.textContent = state;
      // Anything not plainly good reads as a warning, UNKNOWN included: an
      // unchecked component must never look healthy.
      value.className = ["READY", "REGISTERED", "REACHABLE", "ONLINE"].includes(state)
        ? "ok"
        : "warn";
      li.appendChild(name);
      li.appendChild(value);
      if (detail) li.title = detail;
      list.appendChild(li);
    };

    row("Voice", payload.voice, "");
    for (const [key, part] of Object.entries(payload.parts)) {
      row(key.replace(/_/g, " "), part.state, part.detail);
    }
  }

  el("test-call").addEventListener("click", async () => {
    const button = el("test-call");
    button.disabled = true;
    button.textContent = "ringing…";
    const { status, payload } = await api("/api/voice/test-call", { method: "POST" });
    button.textContent =
      status === 200
        ? `rang ${payload.call.push_delivered} phone(s)`
        : payload.error || "could not ring";
    setTimeout(() => {
      button.disabled = false;
      button.textContent = "Test call";
    }, 4000);
  });

  refreshHealth();
  setInterval(refreshHealth, 30000);
  refreshPending();
  // Opened from a notification: skip the ringing screen only if asked to.
  if (new URLSearchParams(location.search).get("answer") === "1") answer();
})();
