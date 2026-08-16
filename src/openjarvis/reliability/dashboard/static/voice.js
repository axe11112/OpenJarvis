/* Sir Voice — the browser half of a call.
 *
 * Three things here are worth knowing before changing anything.
 *
 * 1. Audio is captured as raw PCM through an AudioWorklet and encoded to a
 *    16 kHz mono WAV in this file. The obvious alternative, MediaRecorder,
 *    produces WebM/Opus, which whisper.cpp cannot read — decoding it would put
 *    ffmpeg on the Mac. Encoding a WAV here is about sixty lines and adds no
 *    dependency to the machine that has to stay up.
 *
 * 2. A turn is push-to-talk by default, with automatic end-of-speech detection
 *    while the button is held. The operator is often outdoors, in a room with a
 *    television, or on a phone held loosely; a system that decides on its own
 *    when speech began is a system that transcribes the television.
 *
 * 3. Every failure is recoverable in place. A session that expires while the
 *    phone was asleep answers 409 with `reconnect: true`, and this file picks
 *    the call back up rather than leaving a dead screen.
 */
(() => {
  "use strict";

  const TOKEN = document.body.dataset.controlToken;
  const SAMPLE_RATE = 16000;
  const SILENCE_RMS = 0.012;        // below this counts as quiet
  const SILENCE_MS = 900;           // quiet for this long ends a turn
  const MAX_UTTERANCE_MS = 20000;   // hard stop, so a stuck button cannot run on

  const el = (id) => document.getElementById(id);
  const ringing = el("ringing");
  const call = el("call");
  const state = el("state");
  const transcript = el("transcript");

  let sessionId = null;
  let audioCtx = null;
  let stream = null;
  let capture = null;
  let chunks = [];
  let recording = false;
  let silenceSince = 0;
  let startedAt = 0;

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

  /* --------------------------------------------------------- wav encode */

  function encodeWav(buffers, sampleRate) {
    let length = 0;
    for (const b of buffers) length += b.length;
    const wav = new ArrayBuffer(44 + length * 2);
    const view = new DataView(wav);
    const ascii = (offset, text) => {
      for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
    };
    ascii(0, "RIFF");
    view.setUint32(4, 36 + length * 2, true);
    ascii(8, "WAVEfmt ");
    view.setUint32(16, 16, true);          // PCM header size
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, 1, true);           // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);          // bits per sample
    ascii(36, "data");
    view.setUint32(40, length * 2, true);

    let offset = 44;
    for (const buffer of buffers) {
      for (let i = 0; i < buffer.length; i++, offset += 2) {
        const sample = Math.max(-1, Math.min(1, buffer[i]));
        view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      }
    }
    return new Blob([wav], { type: "audio/wav" });
  }

  function downsample(input, from, to) {
    if (to >= from) return input;
    const ratio = from / to;
    const out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      // Average the source window rather than picking one sample: plain
      // decimation aliases, and aliased speech transcribes badly.
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += input[j];
      out[i] = end > start ? sum / (end - start) : 0;
    }
    return out;
  }

  /* ------------------------------------------------------------ capture */

  async function ensureMic() {
    if (stream) return true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      state.textContent = "this browser will not give me the microphone here";
      el("footnote").textContent =
        "A microphone needs a secure page. Open Sir over https on your Tailscale name.";
      return false;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
    } catch (err) {
      state.textContent = "I need permission to use the microphone";
      return false;
    }
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = audioCtx.createMediaStreamSource(stream);
    // ScriptProcessor is deprecated but universally available, including on
    // the iOS Safari versions this has to run on. The AudioWorklet path needs
    // a separate module file for no behavioural gain at this buffer size.
    capture = audioCtx.createScriptProcessor(4096, 1, 1);
    capture.onaudioprocess = (event) => {
      if (!recording) return;
      const input = event.inputBuffer.getChannelData(0);
      chunks.push(downsample(new Float32Array(input), audioCtx.sampleRate, SAMPLE_RATE));

      let sum = 0;
      for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
      const rms = Math.sqrt(sum / input.length);
      const now = performance.now();
      if (rms > SILENCE_RMS) silenceSince = now;
      if (now - silenceSince > SILENCE_MS && now - startedAt > 1200) stopTurn();
      if (now - startedAt > MAX_UTTERANCE_MS) stopTurn();
    };
    source.connect(capture);
    capture.connect(audioCtx.destination);
    return true;
  }

  async function startTurn() {
    if (recording || !sessionId) return;
    if (!(await ensureMic())) return;
    if (audioCtx.state === "suspended") await audioCtx.resume();
    chunks = [];
    recording = true;
    startedAt = silenceSince = performance.now();
    el("talk-label").textContent = "listening…";
    document.body.classList.add("listening");
  }

  async function stopTurn() {
    if (!recording) return;
    recording = false;
    document.body.classList.remove("listening");
    el("talk-label").textContent = "Hold to talk";
    const captured = chunks;
    chunks = [];
    if (!captured.length) return;

    state.textContent = "thinking…";
    const wav = encodeWav(captured, SAMPLE_RATE);
    const { status, payload } = await api(
      `/api/voice/utterance?id=${encodeURIComponent(sessionId)}`,
      { method: "POST", body: wav, type: "audio/wav" }
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
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
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
  el("decline").addEventListener("click", () => { ringing.hidden = true; endCall(); });
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

  refreshPending();
  // Opened from a notification: skip the ringing screen only if asked to.
  if (new URLSearchParams(location.search).get("answer") === "1") answer();
})();
