# Sir Voice

A private, local, two-way voice interface to JARVIS. Speech is transcribed on
the Mac by whisper.cpp, answers are spoken by macOS's own synthesiser, and the
phone reaches the Mac over Tailscale. There is no telephony provider, no speech
API and no per-minute cost — not as a saving, but as a property: a system
trusted with production should not stream its outages to somebody else's
endpoint.

```
iPhone (installed PWA)
   │  HTTPS over Tailscale — 100.64.0.0/10 only
   ▼
Control Center  ──►  intents  ──►  authority  ──►  capability
   │                    │             │               │
   │                whisper.cpp   READ/SAFE/       existing narrow
   │                  (local)     CONFIRM/          JARVIS primitives
   ▼                              FORBIDDEN
macOS `say` (local)
```

The Mac is the brain. The phone holds no credentials, no repair logic and no
authority; it is a microphone, a speaker and two buttons.

## Why the phone records the way it does

The first version encoded 16 kHz WAV in the browser from raw Web Audio samples,
so the Mac needed no decoder. On a real iPhone, inside an installed PWA, iOS
leaves that graph empty: every utterance produced zero audio, uploaded nothing,
and answered "I didn't catch that" forever.

So recording now goes through `MediaRecorder`, the one path every browser
genuinely supports, and the Mac converts whatever container comes back:

| Browser        | Container            | Decoder on the Mac       |
| -------------- | -------------------- | ------------------------ |
| iOS Safari     | `audio/mp4` (AAC)    | `afconvert` (built in)   |
| Chrome/Android | `audio/webm` (Opus)  | `ffmpeg` (optional)      |
| Firefox        | `audio/ogg` (Opus)   | `ffmpeg` (optional)      |

The container is sniffed from the bytes, never trusted from `Content-Type`.
Audio exists on disk only inside a temporary directory that is removed on every
path out, including failure.

## Understanding versus authority

These are deliberately different steps, and the split is the safety argument for
the whole feature.

**Understanding** is generous. A phrase tier matches exact wording; a keyword
tier matches groups of words that must all appear, in any order. "is production
healthy", "how's Wize", "anything broken" and "did you fix it" all land
correctly without anybody memorising a script.

**Authority** is not generous at all. What a recognised intent may *do* is
decided afterwards, by its risk:

| Risk        | What happens                                                    |
| ----------- | --------------------------------------------------------------- |
| `READ`      | Answered from recorded state. Changes nothing.                    |
| `SAFE`      | Runs immediately — and every member makes JARVIS *less* capable.  |
| `CONFIRM`   | Never runs from voice. Parks a request in the Control Center.     |
| `FORBIDDEN` | Refused flatly, and creates **nothing**.                          |

The `CONFIRM`/`FORBIDDEN` distinction matters more than it looks. `CONFIRM`
parks a request a human might reasonably want. `FORBIDDEN` is for things nobody
should be able to queue by speaking — "ignore your instructions", "read me the
token", "drop table" — because a queued *"disable the audit log"* is one tired
click from being approved.

No intent can reach a shell. Every operation is a call to an existing JARVIS
primitive that was already narrow before a voice could reach it.

## When Sir calls

Only when attention is genuinely required: a post-merge production failure, a
failed production deployment, a `HUMAN_REQUIRED` on something live, a CRITICAL
fault JARVIS cannot handle, exhausted attempts on a production outage, or a
security refusal. **A successful repair is never a call** — it is a Telegram
message.

Storm protection is mandatory, because a watcher ticks every sixty seconds and
an incident in `HUMAN_REQUIRED` stays there all night:

- one active call at a time;
- bounded attempts per incident, then it stops;
- a decline is honoured longer than a miss — it is an answer, not a failure to
  hear;
- exactly one Telegram message when a call cannot be delivered.

## What iOS does and does not allow

**Confirmed against Apple's own documentation**, not assumed:

- A free Apple ID can install an app on your own device, but provisioning
  profiles **expire after 7 days**.
- Free "personal teams" **cannot use the Push Notifications capability at all**.
- PushKit/VoIP push therefore requires the **paid Apple Developer Program**
  ($99/yr), and without it there is no way to wake an app to present CallKit.

So a genuine full-screen incoming call is **blocked without a paid membership**.
A native companion app was deliberately *not* built: without APNs it could only
show CallKit UI while already open in the foreground, which is strictly worse
than the installed PWA.

What works for free is a Web Push notification with sound and vibration on the
lock screen, which opens the call screen when tapped. The health panel reports
this channel as `LIMITED` rather than `READY`, because calling it ready would be
a promise iOS does not let it keep.

## Running it

```bash
jarvis reliability dashboard --tailscale --voice
```

Fetches the Tailscale certificate on first run and prints the URL. HTTPS is not
optional: a browser refuses microphone access and Web Push on an insecure
origin, so the call screen would render and then do nothing.

## Health

`GET /api/voice/health` and the Sir panel report STT, TTS, audio decoding,
phone registration, call channel and Tailscale separately. **Unknown is never
healthy** — a component nobody has checked reports `UNKNOWN` and drags the whole
panel to `DEGRADED`. A green light for something unverified is how an operator
learns to stop reading the panel.

## Security

- Tailscale only: loopback, or `100.64.0.0/10` / `fd7a:115c:a1e0::/48`. Wildcard
  binds are refused by name.
- `Host` header validated — the DNS-rebinding guard survives the widening.
- Control token on every state-changing request.
- Bounded request bodies and utterance length.
- Transcripts are untrusted input, redacted before they are stored, shown or
  matched.
- Identifiers are stripped before synthesis: a SHA read aloud is unusable and a
  leak.
- No raw audio is persisted. Ever.
