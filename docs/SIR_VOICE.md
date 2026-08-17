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

Recording therefore goes through `MediaRecorder`, the one path every browser
genuinely supports. Uploading its output unchanged, however, only moved the
failure to the Mac. Safari with a timeslice emits a **fragmented** MP4 — a
header chunk followed by `moof`/`mdat` fragments — and `afconvert`, the only
decoder macOS ships and the only one on a Mac without Homebrew, will not read
it. The upload succeeded, the conversion failed, and every utterance still came
back "I didn't catch that".

So the phone now decodes its own recording and uploads plain 16 kHz mono WAV:

1. `MediaRecorder` captures, with **no timeslice** — one finalised container.
2. `decodeAudioData` decodes it. This is the same decoder that plays the clip
   back, so it can always read what the browser just wrote.
3. `OfflineAudioContext` resamples to 16 kHz mono.
4. A 44-byte RIFF header is written and the WAV is uploaded.

This is not the ScriptProcessor mistake repeated. That starved a *live* audio
graph, which iOS does not keep fed inside an installed PWA; these render as fast
as they can with no realtime clock to miss.

If any step fails the original container is uploaded unchanged and the Mac tries
`afconvert`/`ffmpeg` as before — a best-effort improvement must never become a
new way to lose an utterance. The container is sniffed from the bytes, never
trusted from `Content-Type`, and audio exists on disk only inside a temporary
directory removed on every path out, including failure.

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

`GET /api/voice/health` and the Sir panel report the microphone, STT, TTS,
audio decoding, phone registration, call channel and Tailscale separately.
**Unknown is never healthy** — a component nobody has checked reports `UNKNOWN`
and drags the whole panel to `DEGRADED`. A green light for something unverified
is how an operator learns to stop reading the panel.

### The microphone is not the speech engine

These are two lights because they fail independently, and conflating them
shipped a Control Center that read `ONLINE` while every word spoken into the
real iPhone came back as "I didn't catch that". Every component was healthy. The
product did not work, because *installed* was being read as *working*.

| Light | Question it answers |
| --- | --- |
| **STT engine** — `READY` / `FAILED` | Does whisper load, with a model file? |
| **Microphone** — `UNKNOWN` / `WORKING` / `FAILED` | Has a real phone ever actually been heard? |

The microphone reaches `WORKING` on exactly one kind of evidence: a real device,
over the network, sending audio that produced a transcript. A synthesised WAV
from the test suite proves the library works, which was never the thing in
doubt, and is ignored.

**Voice does not report `ONLINE` until the microphone is `WORKING`.**

`FAILED` needs audio that had sound in it, three times in a row, or audio that
could not be decoded at all. Silence and half-second taps stay `UNKNOWN`: the
owner may simply not have spoken, and calling that a failure is the same
overclaim in the other direction. One bad utterance never condemns a microphone
that has worked.

The record is persisted to `~/.openjarvis/voice/microphone.json`, because "has
this ever actually worked?" must survive the restart that follows every deploy.
No audio and no transcript text is written — only a word count.

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
