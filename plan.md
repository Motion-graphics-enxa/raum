# RAUM: build plan

> **Translates. Checks. Forgets.**
> A private AI interpreter that lives in the room. It cleans the audio, translates both ways, checks every translation and writes the minutes. Nothing leaves the box.

| | |
|---|---|
| Event | Macromedia Berlin Hackathon 2026, Mehringdamm 33, Studio One |
| Dates | Mon 2 Nov and Tue 3 Nov 2026 (venue 09:00–18:30; we build straight through the night) |
| Theme | Reimagine the Future: AI, technology and creativity for tomorrow's world |
| Prizes | €500 / €100 / €50; top 5 considered for funding grants and LEXIE |
| Judging | Problem & Purpose · Creativity & Originality · Execution · Human Value & Impact · Cohesion & Experience |
| Hardware | **ASUS Ascent GX10** (NVIDIA GB10 Grace Blackwell, 128 GB unified memory, ARM64; same platform as the DGX Spark, which this plan calls "the Spark"). LAN MAC `A0:AD:9F:DD:04:54`, Wi-Fi MAC `50:BB:B5:A3:ED:7A`, 48 V / 5 A (240 W) adapter. On slides, say "NVIDIA GB10 box" or "ASUS Ascent GX10", not "DGX Spark" |
| Product brief | https://claude.ai/artifact/LHsWfyX7bsQ9QT11LLfd3t |
| Repository | https://github.com/TheWingTheWong/raum (private until submission) |
| Design skill | `impeccable` only (see §4) |

**How to use this document.** §1 is the one-screen summary. §2–§4 define the product and experience. §5–§7 define the system. §8–§9 are runbooks with commands. §10–§11 are the schedule and task board. §12–§17 cover proof, demo, pitch, risks and submission. §18 lists what the humans must do so Claude Code can do the rest.

---

## Contents

1. [Summary](#1-summary)
2. [Product](#2-product)
3. [Experience principles](#3-experience-principles)
4. [Design system with impeccable](#4-design-system-with-impeccable)
5. [Feature catalogue](#5-feature-catalogue)
6. [Architecture](#6-architecture)
7. [Repository layout and stack](#7-repository-layout-and-stack)
8. [Spark runbook](#8-spark-runbook)
9. [Network, HTTPS and remote runbook](#9-network-https-and-remote-runbook)
10. [Schedule and gates](#10-schedule-and-gates)
11. [Task board](#11-task-board)
12. [Evaluation and measurement](#12-evaluation-and-measurement)
13. [Demo script](#13-demo-script)
14. [Pitch, slides and video](#14-pitch-slides-and-video)
15. [Q&A bank](#15-qa-bank)
16. [Risk register](#16-risk-register)
17. [Submission and packing checklists](#17-submission-and-packing-checklists)
18. [What the humans must do](#18-what-the-humans-must-do)
19. [Open decisions](#19-open-decisions)
20. [Appendix: prompts, schemas, test data](#20-appendix-prompts-schemas-test-data)

---

## 1. Summary

**What we build.** RAUM, a box (the Spark) that interprets between people who don't share a language. Everyone joins with their own device by scanning a QR code. The box cleans each voice, transcribes it, translates it, checks the translation with code and with a second model, speaks it, and writes a bilingual summary at the end. Then it burns the session. Every model runs locally.

**Who it is for.** Three rooms where a leak is a legal problem:

| Room | People | Signature |
|---|---|---|
| **Sprechzimmer** (leads the pitch) | Doctor or clinic, and patient | Dose and allergy guard, repeat-back check, take-home summary |
| **Kanzlei** | Lawyer and client | Deadline guard, "explain simply" approved by the lawyer, case notes |
| **Verhandlungsraum** | CEO and foreign partner | Offer tracker, deal glossary, minutes both sides approve |

**Why we win.** Most teams will show slides or Figma. We show working hardware, a human story, measured numbers and a privacy proof the jury can watch. We pull the internet cable and it keeps working.

**The lines.**
- *"Any AI can translate. RAUM checks."*
- *"Seven models, one box, zero bytes to third parties."*
- *"Works from anywhere. Works with nothing."*

**Positioning.** On stage it is a product: Amira, the box, three rooms. Underneath it is an open framework (any device, any hardware, rooms as plugins). The framework appears in one demo beat, one slide, the business model and Q&A.

**Deadlines.** One continuous sprint, through the night. **T0 = the moment Claude Code can SSH into the Spark.** If T0 is about 13:00 Monday, the product is finished around 02:00 Tuesday.

| Gate | When | Must be true |
|---|---|---|
| G1 | T0 + 1 h | Ollama, Whisper and LiveKit answer on the Spark; a phone joins an empty room |
| G2 | T0 + 3 h | **First full turn:** phone A speaks, phone B shows the translation |
| G3 | T0 + 5 h | Fact guard, check badges and glass box work live |
| G4 | T0 + 8 h | Burn, audit log, privacy meter, tunnel, voice, Sprechzimmer complete |
| G5 | T0 + 11 h | Kanzlei, Verhandlungsraum, document camera, load test. **Feature freeze** |
| G6 | T0 + 13 h | Polish, proof numbers, backup video, slides. **Code freeze: product ready** |

---

## 2. Product

### 2.1 Names and lines

- **Name:** RAUM (German for "room").
- **Slogan:** Translates. Checks. Forgets.
- **Tagline:** What's said in the room stays in the room.
- **One-liner:** A private AI interpreter that lives in the room. It cleans the audio, translates both ways, checks every translation and writes the minutes. Nothing leaves the box.

### 2.2 The problem (verified facts, sources in the brief)

- Statutory health insurance does not pay for interpreters. Patients pay or go without.
- Only 14 of 38 German university hospitals can get an interpreter at weekends.
- About 15% of people in Germany mainly speak another language at home.
- Berlin's medical association demanded insurer coverage in December 2025.
- **§203 StGB:** disclosing patient or client secrets is a crime for doctors and lawyers.
- **§43e BRAO:** lawyers may use outside IT services only under strict conditions, with extra hurdles for providers abroad.
- **GeschGehG §2:** information is a protected trade secret only if the company takes appropriate confidentiality measures.
- Consumer earbud translation covers about nine languages and checks nothing.

### 2.3 Stories

- **Amira (opening story, not a statistic):** at the doctor, her 12-year-old son translates her diagnosis. He doesn't know the words, and she doesn't know what he left out.
- **Kanzlei:** a client explains an employment dispute in her own language; the lawyer gets German case notes with the deadline flagged.
- **Verhandlungsraum:** a Berlin CEO negotiates with a supplier abroad; prices are tracked, nothing touches a server.

### 2.4 Non-goals (say them out loud)

- No lie detection, no emotion analysis, no hidden tactics advice for one side.
- Not a medical device: RAUM is a communication aid and makes no diagnosis.
- No engagement tricks: no streaks, no nagging notifications.
- For the hackathon: no native iOS app, no single-use guest links, no session hand-over between devices.

---

## 3. Experience principles

These eight rules govern every screen, sound and sentence. Review every UI change against them.

| # | Rule | What it means in the build |
|---|---|---|
| 1 | **One scan, zero decisions** | No account. Language from `navigator.language`. Room preset by the box. Role picked by which QR you scan |
| 2 | **Wow in ten seconds** | QR scan to first translated sentence in under 10 s. Measured |
| 3 | **Never a dead second** | Live waveform while listening, captions while speaking, translation typing in, tick when verified |
| 4 | **Trust you can see** | Green, amber and red badges; third-party bytes counter at 0; visible burn |
| 5 | **Leave with something** | Every conversation ends on the summary: note, case notes or minutes, plus the take-home copy |
| 6 | **Better every time** | Glossary and presets persist on the box per practice |
| 7 | **Installs like a toaster** | Plug in, or one command. A room is one file |
| 8 | **Three screens only** | Join, Talk, Summary on phones. Table screen and Admin on the laptop |

**Habit loop:** trigger (a patient who doesn't speak German) → one action (scan, Start) → reward (understood, verified, paperwork done) → investment (glossary and presets grow).

**Journey (professional / other party):** discover → set up (plug in, scan sticker, three questions) → join (one tap) → first words (under 1 s, green tick) → during (flags only when they matter) → end (summary in seconds) → close (burn, "key destroyed") → after ("RAUM wrote 14 notes for you this week", counted on the box) → return (presets waiting) → spread (patients ask for RAUM).

**Speed targets** (measured and shown live on the glass box):

| Stage | Target |
|---|---|
| Audio reaches the box | ~50 ms |
| End of sentence detected | 300–400 ms |
| Transcribed | 150–300 ms |
| First translated words on the listener's screen | **< 1.0 s** after the speaker stops |
| Full translated sentence | < 1.5 s |
| Voice starts in the listener's ear | < 1.5 s |
| Check badge | +~1 s, never blocks |

Fallback if streaming is not tuned in time: full sentence in 1–2 s.

---

## 4. Design system with impeccable

`impeccable` is the only skill we use. It owns the visual world, the UI copy and the final quality passes.

### 4.1 Surfaces and modes

| Surface | Route | impeccable mode | Device |
|---|---|---|---|
| Join | `/j/:room` | Operate | Phone |
| Talk | `/r/:room` | Operate | Phone |
| Summary | `/r/:room/summary` | Operate | Phone and laptop |
| Table screen (conversation + glass box) | `/t/:room` | Operate | Laptop or monitor |
| Admin console | `/admin` | Operate | Laptop |
| Landing page | `/` (static) | Persuade | Any |

### 4.2 Workflow

1. **Setup (once per session):** run `~/.claude/skills/impeccable/scripts/impeccable context` from the repo root.
2. **`/impeccable init`** (Sprint 0): writes `PRODUCT.md` from §2 and §3 of this plan.
3. **`/impeccable shape talk`** (Sprint 0): plans the Talk screen and the room-specific panels.
4. **New-work visual world** (Sprint 1): writes `DESIGN.md` with tokens. One identity across app, table screen, slides, video and landing page. Each room gets an icon and an accent within the same system.
5. **Build** screens against `DESIGN.md` (Sprints 1–4). Read `reference/craft-floor.md` before each UI edit, as the skill requires.
6. **`/impeccable onboard join`** (Sprint 2): first-run flow and the consent screen.
7. **`/impeccable critique`** and **`/impeccable audit`** (Sprint 5, right after feature freeze): one batched round, desktop and mobile.
8. **`/impeccable polish`** (Sprint 5): final pass, then stop. No open-ended polishing loops.

### 4.3 Design constraints to hand to impeccable

- Large readable type; works for older patients; high contrast in both themes.
- Semantic colours for verified, unsure and mismatch are separate from the brand accent.
- Talk screen: one dominant talk control; the other person's words in your language are the hero.
- Glass box must read from 3 metres away on the table screen.
- Motion: subtle, respects `prefers-reduced-motion`; one signature moment (the burn).
- Signature tick sound when a sentence is verified; vibration on Android only (iOS Safari has no Vibration API).

---

## 5. Feature catalogue

Priority: **P0** must ship · **P1** ship if the gate before it passed · **P2** stretch · **P3** roadmap slide only.

### 5.1 Core

| ID | Feature | P | Acceptance |
|---|---|---|---|
| C-01 | QR join with role and room encoded in the URL | P0 | Phone opens `/j/:room?role=b`, joins without typing |
| C-02 | Language from phone settings, editable with one tap; remembered per device | P0 | `navigator.language` preselected; persists across reloads |
| C-03 | Consent screen: "This is an AI interpreter" (EU AI Act Art. 50); recording off | P0 | Must tap to continue; consent written to audit log |
| C-04 | Mic capture over WebRTC (LiveKit), echo cancellation on | P0 | Audio track visible in LiveKit room |
| C-05 | Noise cleaning (DeepFilterNet) per speaker | P0 | A/B recording shows hall noise removed |
| C-06 | End-of-speech detection (Silero VAD) plus optional push-to-talk | P0 | Turn closes within 400 ms of silence |
| C-07 | Transcription (Whisper large-v3-turbo) with partial captions while speaking | P0 | Partial text updates about every second; final on endpoint |
| C-08 | Streaming translation to every other participant's language | P0 | Tokens arrive on phone B as they generate |
| C-09 | Shared table screen: bilingual conversation left, glass box right | P0 | Readable from 3 m |
| C-10 | Off-the-record button | P0 | Pipeline drops audio while active; both phones grey |
| C-11 | Bilingual summary from the room template; both sides approve; PDF export | P0 | Summary in < 10 s after End |
| C-12 | Reconnect and resume: session lives on the box | P0 | Kill Wi-Fi on a phone for 10 s; conversation continues |
| C-13 | Voice output (Piper first, Chatterbox if time) | P1 | First audio < 1.5 s after speaker stops |
| C-14 | Room-wide mic pause while voice output plays (echo control) | P1 | No re-translation loops with speakers on |

### 5.2 Verification

| ID | Feature | P | Acceptance |
|---|---|---|---|
| V-01 | **Fact guard**: numbers, units, dates, names, medication names compared by code | P0 | 100% of perturbed-number test cases flagged red |
| V-02 | **Second-model check**: gpt-oss back-translates; bge-m3 scores meaning; badge green/amber/red | P0 | Catches ≥ 80% of semantic perturbations; < 10% false alarms on clean set (calibrated) |
| V-03 | Uncertainty flag from transcription confidence: "please say that again" | P0 | Mumbled test utterance triggers the prompt |
| V-04 | Conversation memory: names, terms, Sie/du consistent across turns | P1 | Name spelled once stays identical |
| V-05 | Live corrections by the professional apply for the rest of the session | P1 | Correct a term once; next occurrence uses it |

### 5.3 Trust and compliance

| ID | Feature | P | Acceptance |
|---|---|---|---|
| T-01 | Session encryption: AES-256-GCM, key in memory only | P0 | Files on disk unreadable without the key |
| T-02 | **Burn by key deletion**: key overwritten, files deleted, visible animation | P0 | After burn, files gone, key gone, audit shows `key_destroyed` |
| T-03 | Auto-burn after a set time | P1 | Session burns itself after N minutes idle |
| T-04 | Tamper-proof audit log (SHA-256 hash chain), no content | P0 | `raum audit verify` passes; editing a line breaks the chain |
| T-05 | Compliance export per session (JSON, printable) | P1 | Shows consent, models and versions, times, burn |
| T-06 | **Privacy meter**: "Bytes to third parties: 0" and "Encrypted tunnel to your own devices: X KB" | P0 | Counters update every second on table screen |
| T-07 | **Sealed mode**: firewall drops all outbound traffic from RAUM services | P1 | Toggle on; meter shows "outbound blocked" |

### 5.4 Glass box

| ID | Feature | P | Acceptance |
|---|---|---|---|
| G-01 | Each sentence animates through stages: heard, cleaned, transcribed, translated, checked, spoken | P0 | Stage timings shown per utterance |
| G-02 | Model names and live GPU load | P0 | Updates every second |
| G-03 | Connection badge per device: Direct, Tunnel, Relayed, with ms | P0 | Remote teammate shows "Tunnel" |
| G-04 | Latency p50/p95 for the session | P1 | Visible on table screen |

### 5.5 Rooms

| ID | Room | Feature | P |
|---|---|---|---|
| S-01 | Sprechzimmer | Dose, frequency and allergy guard with read-back confirmation | P0 |
| S-02 | Sprechzimmer | Repeat-back check: patient restates; AI compares with the doctor's instruction | P0 |
| S-03 | Sprechzimmer | Take-home summary on the patient's phone via QR (meds, next appointment, warning signs) | P0 |
| S-04 | Sprechzimmer | German note for the patient file | P0 |
| S-05 | Sprechzimmer | "Request a human interpreter" button | P0 |
| K-01 | Kanzlei | Deadline and date guard | P0 |
| K-02 | Kanzlei | "Explain simply", approved by the lawyer before sending | P1 |
| K-03 | Kanzlei | Case notes in German: facts, people, dates, deadlines | P0 |
| N-01 | Verhandlungsraum | Offer tracker (timeline of every amount) | P0 |
| N-02 | Verhandlungsraum | Deal glossary upload | P1 |
| N-03 | Verhandlungsraum | Minutes: agreed, open, next steps; both approve | P0 |

### 5.6 Devices, anywhere, framework

| ID | Feature | P |
|---|---|---|
| A-01 | Any phone via browser (PWA) | P0 |
| A-02 | iPhone with AirPods or paired hearing aids (uses the phone's audio route) | P0 (free) |
| A-03 | USB or Bluetooth speakerphone plugged into the Spark as a participant (PipeWire) | P1 |
| A-04 | Private tunnel for team devices (Tailscale) | P0 |
| A-05 | Shared ward tablet (any browser) | P0 (free) |
| A-06 | Laptop relay node | P2 |
| F-01 | Many rooms at once with a GPU priority queue | P0 |
| F-02 | `raum.yaml` model registry; room plugins `modes/*.yaml` | P0 |
| F-03 | `install.sh` with hardware profiles `spark` and `lite` proven | P1 |
| F-04 | Event API (WebSocket + REST, OpenAPI docs) | P1 |
| F-05 | Admin console: devices, rooms, GPU, latency, privacy meter, seal, burn-all | P1 |
| F-06 | Load test across 1, 2, 4, 8, 12 rooms | P1 |
| F-07 | Document camera (Gemma 4 vision) for letters, contracts, term sheets | P1 |
| F-08 | Tauri desktop app (Mac, Windows, Linux) | P2 |
| F-09 | Android APK | P2 |
| F-10 | Voice lock (SpeechBrain ECAPA) | P2 |
| F-11 | Three people, three languages | P2 |
| R-01 | Self-hosted NetBird instead of Tailscale | P3 |
| R-02 | Single-use guest links via a blind relay | P3 |
| R-03 | Native iOS app, Jetson `edge` profile, certified box | P3 |

---

## 6. Architecture

### 6.1 Overview

```
 Phones / laptop / speakerphone                                   Table screen (laptop)
   │  WebRTC audio (Opus), echo cancellation                            ▲ events
   ▼                                                                    │
┌─────────────────────────────── DGX Spark ───────────────────────────────────────────┐
│ Caddy :443 (app, API)  :7443 (LiveKit signalling)   ── TLS (local CA or tailnet cert) │
│ LiveKit server :7880 / :7881 tcp / :7882 udp   (rooms, tracks, data channel)          │
│                                                                                       │
│ raum-agent (Python, LiveKit Agents)  one hidden participant per room                  │
│   per speaker track: 48 kHz → DeepFilterNet → Silero VAD → 16 kHz buffer               │
│   → Whisper (partial every ~1 s, final on endpoint)                                    │
│   → translate per target language (Gemma 4, streamed)                                  │
│   → publish events + per-language voice track                                         │
│   → async: fact guard (code) + back-translation (gpt-oss) + bge-m3 similarity          │
│                                                                                       │
│ raum-core (FastAPI :8000): tokens, rooms, modes, sessions, encryption, audit, summary, │
│   privacy meter, admin, event API                                                      │
│ scheduler: GPU priority queue (live > checks > summaries/vision)                       │
│                                                                                       │
│ Ollama :11434   gemma4:26b · gpt-oss:120b · bge-m3                                     │
│ whisper.cpp server :8081   large-v3-turbo (CUDA)                                       │
│ TTS: Piper (CPU) / Chatterbox (NGC container, GPU)                                     │
│ nftables table "raum": counters + sealed mode        Tailscale: tunnel + tailnet cert  │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Components and ports

| Component | Runs as | Port(s) | Notes |
|---|---|---|---|
| Caddy | Docker or apt | 443, 7443 | Local CA (`tls internal`) or tailnet cert |
| LiveKit server | Docker, host network | 7880 http/ws, 7881 tcp, 7882 udp | `--dev` keys for the hackathon, `--node-ip <LAN IP>` |
| raum-agent | systemd, user `raum` | none | LiveKit Agents worker |
| raum-core | systemd, user `raum` | 8000 (localhost) | FastAPI |
| Ollama | systemd, user `ollama` | 11434 (localhost) | `OLLAMA_KEEP_ALIVE=-1` |
| whisper-server | systemd, user `raum` | 8081 (localhost) | CUDA build |
| Piper / Chatterbox | in-process / container | 8082 | Chatterbox time-boxed |
| Tailscale | systemd | 41641 udp | `tailscale serve` optional |
| App (React build) | served by Caddy | 443 | PWA |

### 6.3 Model lineup

| Role | Model | Memory | Licence | Why |
|---|---|---|---|---|
| Speech detection | Silero VAD | tiny | MIT | Fast, CPU, ONNX |
| Noise cleaning | DeepFilterNet3 (ONNX) | tiny | MIT / Apache 2.0 | ~30 ms, CPU, 48 kHz native |
| Transcription | Whisper large-v3-turbo (whisper.cpp) | ~1.6 GB | MIT | ~100 languages, language ID |
| **Translation + document vision** | **Gemma 4 26B-A4B** (`gemma4:26b`) | ~17–19 GB | **Apache 2.0** | MoE, ~3.8B active; reported ~40 tok/s on Spark (Ollama), ~52 tok/s (vLLM NVFP4); 140+ languages pretrained; reads images |
| Second-model check, summaries, explain simply, repeat-back | gpt-oss-120b | ~65 GB | Apache 2.0 | **Different family from the translator**, so its errors are independent |
| Meaning comparison | bge-m3 (Ollama embeddings) | ~1.2 GB | MIT | Multilingual embeddings |
| Voice output | Piper (CPU) / Chatterbox Multilingual (GPU) | ~0.1 / ~2 GB | MIT (check each Piper voice) | Piper is the safe default |
| **Total loaded at once** | | **~88–90 GB** of 128 GB | | A laptop cannot do this |

**Why Gemma 4 for translation.** Released April 2026 under Apache 2.0 (earlier Gemma generations had a custom licence). Natively multilingual, the 26B MoE runs fast on the Spark, and it handles images, so the document camera needs no extra model. The 31B dense model is too slow for live use on the Spark (~4–7 tok/s reported).

**Why not Gemma for the checker too.** If translator and checker share a family, they share blind spots. The second opinion must come from a different family.

**Model bake-off (Sprint 1, ~40 min).** We pick the translator by measurement, not opinion. One line in `raum.yaml` swaps it.

| Candidate | Why test it |
|---|---|
| `gemma4:26b` | Default |
| `gemma4:e4b` | Faster; check if quality holds for our languages |
| TranslateGemma 12B (if a GGUF/Ollama build is available) | Translation specialist (Jan 2026, 55 languages). **Gemma Terms of Use, not Apache**: fine for the demo, flag for the business |
| `qwen3:30b-a3b` | Baseline |

Score on the 30-sentence test set (§12): fact-guard pass rate, back-translation similarity, native-speaker rating 1–5, first-token and full-sentence latency. The winner goes into `raum.yaml`; the table goes on the proof slide.

**Avoided:** NLLB, SeamlessM4T, Coqui XTTS (non-commercial licences).

### 6.4 Turn lifecycle

```
t=0      speaker stops talking
+300 ms  VAD endpoint (silence threshold, tuned)              → event utterance.end
+500 ms  Whisper final (partials were already on screen)       → transcript.final
+700 ms  first translated tokens on listener screen            → translation.delta
+1.3 s   full sentence                                         → translation.final
+1.3 s   voice output starts on first sentence (C-13)          → audio track
+1.3 s   fact guard runs (code, < 5 ms)                        → verify.facts
+2.3 s   back-translation + similarity                         → verify.result (badge)
```

Rules:
- Checks never block display. Only a **red** result interrupts with a confirm prompt.
- Partials run at lower GPU priority than finals.
- Translation prompt prefix stays fixed per room so the runtime can reuse its cache.
- Models are warmed at boot with a dummy request.

### 6.5 Event protocol

Transport: LiveKit data channel, topic `raum.events`, reliable mode, JSON. The same events are mirrored to `ws://localhost:8000/api/events/:room` for the table screen and the event API.

```jsonc
// common envelope
{ "v": 1, "type": "translation.delta", "room": "sz-4821", "utt": "u-0007",
  "ts": 1793616884123, "data": { ... } }
```

| Type | Data |
|---|---|
| `participant.joined` | `{ id, role, lang, device, path: "direct"|"tunnel"|"relayed" }` |
| `utterance.start` / `utterance.end` | `{ speaker, lang }` |
| `transcript.partial` | `{ speaker, lang, text, seq }` |
| `transcript.final` | `{ speaker, lang, text, asr_conf }` |
| `translation.delta` | `{ target_lang, delta }` |
| `translation.final` | `{ target_lang, text }` |
| `verify.facts` | `{ target_lang, facts: [{ kind, src, tgt, match }] }` |
| `verify.result` | `{ target_lang, similarity, badge: "green"|"amber"|"red", reasons: [] }` |
| `flag` | `{ kind: "dose"|"allergy"|"deadline"|"amount"|"uncertain", text, needs_confirm }` |
| `confirm` | `{ by, accepted }` |
| `offer.update` | `{ amount, currency, by, ts }` |
| `pipeline.timing` | `{ vad_ms, asr_ms, mt_first_ms, mt_full_ms, tts_first_ms, check_ms }` |
| `session.state` | `{ off_record, sealed, participants, mode }` |
| `privacy.meter` | `{ third_party_bytes, tunnel_bytes, sealed }` |
| `gpu.stats` | `{ util_pct, models_loaded }` |
| `summary.ready` | `{ template, lang_a, lang_b, sections }` |
| `burn.done` | `{ key_destroyed_at, audit_head_hash }` |

### 6.6 Room config (`modes/sprechzimmer.yaml`)

```yaml
id: sprechzimmer
name: { de: Sprechzimmer, en: Consultation room }
icon: stethoscope
roles:
  a: { label: { de: Ärztin/Arzt, en: Doctor }, default_lang: de }
  b: { label: { de: Patient/in, en: Patient }, default_lang: auto }
register: formal            # Sie
guard:
  kinds: [dose, frequency, allergy, date, number, medication]
  confirm_on: [dose, allergy]
glossary: glossaries/medical-de.yaml
prompts:
  translate: prompts/translate.md
  summary: prompts/summary-sprechzimmer.md
  repeat_back: prompts/repeat-back.md
summary:
  sections: [medications, allergies, next_appointment, warning_signs, open_questions]
  takehome_for: b
  file_note_for: a
buttons: [off_record, repeat_back, human_interpreter, end]
retention: { auto_burn_minutes: 30 }
```

`kanzlei.yaml` and `verhandlungsraum.yaml` follow the same schema with their own guard kinds (`deadline`, `date`, `amount`, `currency`, `percent`), prompts and buttons (`explain_simply`, `offer_tracker`).

### 6.7 `raum.yaml` and profiles

```yaml
profile: spark                       # spark | gpu-24 | mac | edge | lite
models:
  vad:        { engine: silero }
  denoise:    { engine: deepfilternet, onnx: true }
  asr:        { engine: whisper.cpp, url: http://127.0.0.1:8081, model: large-v3-turbo }
  translate:  { engine: ollama, model: gemma4:26b, options: { temperature: 0.1, num_predict: 256 } }
  verify:     { engine: ollama, model: gpt-oss:120b, reasoning: low }
  embed:      { engine: ollama, model: bge-m3 }
  vision:     { engine: ollama, model: gemma4:26b }
  tts:        { engine: piper, fallback: none }
scheduler: { live_slots: 2, check_slots: 1, background_slots: 1 }
security:  { session_cipher: aes-256-gcm, audit_log: /var/lib/raum/audit.jsonl }
network:   { livekit_url: wss://<host>:7443, tunnel: tailscale }
```

| Profile | Hardware | Translate | Verify | ASR |
|---|---|---|---|---|
| `spark` | DGX Spark 128 GB | gemma4:26b | gpt-oss:120b | large-v3-turbo |
| `gpu-24` | RTX 4090/5090 | gemma4:26b | gpt-oss:20b | large-v3-turbo |
| `mac` | Apple Silicon ≥ 32 GB | gemma4:26b-mlx | gpt-oss:20b | large-v3-turbo (Metal) |
| `edge` | Jetson Orin | gemma4:e4b | off | small |
| `lite` | any laptop (e.g. M2 8 GB) | gemma4:e2b | off | base |

### 6.8 Fact guard design (V-01)

1. Extract from source and translation: numbers (locale-aware: `1.200,50` vs `1,200.50`), units (`mg`, `ml`, `µg`, `IE`, `%`, `€`, `EUR`, `USD`), dates and weekdays (`dateparser`, multilingual), times, durations, medication names (glossary list), proper names (capitalised tokens present in both, glossary names).
2. Normalise: decimal separators, thousands separators, number words for 0–20 in the demo languages, unit synonyms (`Milligramm` = `mg`).
3. Compare multisets per kind. Any missing, extra or different value → **red**, with the exact pair shown.
4. Kinds listed in the room's `confirm_on` → confirm prompt on the other device ("500 mg, twice a day. Correct?").
5. Pure Python, no model. Target < 5 ms per utterance. Unit tests in `core/tests/test_guard.py` with the perturbation cases from §12.

### 6.9 Second-model check design (V-02)

1. After `translation.final`, gpt-oss-120b translates the target text back into the source language (literal, low reasoning, `num_predict` 256).
2. bge-m3 embeds source and back-translation; cosine similarity `s`.
3. Initial thresholds, **calibrated in Sprint 3** with the clean and perturbed sets: `s ≥ 0.86` green, `0.78 ≤ s < 0.86` amber, `s < 0.78` red. Fact-guard red always wins.
4. Amber shows "unsure, please confirm" without interrupting; red interrupts.
5. Runs at check priority; never blocks the live path.

### 6.10 Session security (T-01 to T-05)

- On session start: generate a 256-bit key with `os.urandom`, kept in a `bytearray` in raum-core memory only.
- Transcripts and summaries are appended to `/var/lib/raum/sessions/<id>.bin` as AES-256-GCM records (`cryptography` library), nonce per record.
- **Burn:** overwrite the key bytearray with zeros, drop references, delete session files, emit `burn.done`, append `key_destroyed` to the audit log. Honest note for Q&A: Python can't guarantee every copy in RAM is wiped, but without the key the disk copy is useless.
- **Audit log** (`/var/lib/raum/audit.jsonl`): each line `{ts, session, event, meta, prev, hash}` where `hash = sha256(prev + canonical_json(line_without_hash))`. Events: `session_start`, `consent`, `models` (names and digests), `off_record_on/off`, `summary_approved`, `key_destroyed`. **No content ever.**
- `raum audit verify` recomputes the chain. The compliance export renders one session's audit entries.

### 6.11 Privacy meter and sealed mode (T-06, T-07)

- nftables table `raum` with an `output` chain: a named counter for packets from users `raum` and `ollama` (`meta skuid`) to destinations **not** in private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`) and **not** in the tailnet range (`100.64.0.0/10`). This is the **third-party bytes** counter and should stay at 0.
- A `forward` chain counter does the same for Docker bridge subnets (LiveKit, Caddy).
- **Tunnel bytes:** read `/sys/class/net/tailscale0/statistics/{tx,rx}_bytes`.
- **Sealed mode:** the same match with `drop` instead of `counter`, toggled from Admin. Model downloads must happen before sealing.
- raum-core reads counters every second (`nft -j list counters table inet raum`) and emits `privacy.meter`.

### 6.12 Scheduler and capacity (F-01)

- Three queues in raum-core: **live** (finals, translation), **check** (back-translation, embeddings), **background** (partials, summaries, vision). Live always preempts.
- Ollama: `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_MAX_LOADED_MODELS=3`, `OLLAMA_KEEP_ALIVE=-1`.
- Estimated capacity on one Spark, to be measured (F-06): 4–6 conversations at once with full checking; 10+ with gpt-oss-20b as checker or vLLM batching.

### 6.13 Echo control (C-14)

1. When a voice track plays in a room, raum-agent ignores mic audio from devices in that room for the playback duration plus 300 ms.
2. Browser `echoCancellation: true`. Test `noiseSuppression` both ways (DeepFilterNet may do better alone).
3. Headphones for the demo. Voice lock (F-10) as the stretch fix.

### 6.14 Connectivity

| Situation | Path | Notes |
|---|---|---|
| Same network as the box | Direct over LAN | Use the box's **IP** in QR codes: Android does not reliably resolve `.local` names |
| Team device anywhere | Tailscale tunnel | WireGuard; survives Wi-Fi ↔ mobile data switches |
| Hospital guest Wi-Fi with client isolation | Tailscale tunnel | Goes around the isolation |
| Outside guest | Not in the hackathon (R-02) | Roadmap: single-use link via blind relay |

HTTPS options are in §9.3.

### 6.15 Failure handling

| Failure | What the user sees | What happens |
|---|---|---|
| Voice output fails | Text continues | TTS disabled for the session, banner in Admin |
| Checker slow or down | Grey badge "check pending" | Live translation unaffected |
| Phone drops | "Reconnecting…" then resumes | LiveKit reconnect; session state on the box |
| Remote participant lost (cable pulled) | "Remote participant offline. The room continues locally." | Local conversation unaffected |
| Whisper low confidence | "Please say that again" | Utterance not translated |
| GPU saturated | Latency number turns amber in glass box | Background queue paused |
| Fact mismatch | Red line, confirm prompt | Translation held until confirmed |

---

## 7. Repository layout and stack

```
raum/
├── plan.md                      this document
├── README.md
├── LICENSE                      Apache 2.0
├── PRODUCT.md                   written by /impeccable init
├── DESIGN.md                    written by impeccable new-work
├── raum.yaml                    model registry and profile
├── install.sh                   hardware detection → profile → services
├── core/                        Python 3.12, uv
│   ├── pyproject.toml
│   ├── raum/
│   │   ├── agent/               LiveKit worker: per-room, per-track pipeline
│   │   ├── pipeline/            vad.py denoise.py asr.py translate.py tts.py
│   │   ├── verify/              guard.py backtranslate.py similarity.py
│   │   ├── modes/               loader + schema (pydantic)
│   │   ├── security/            crypto.py burn.py audit.py
│   │   ├── privacy/             meter.py seal.py
│   │   ├── scheduler.py
│   │   ├── summary.py
│   │   └── api/                 FastAPI: tokens, rooms, sessions, events, admin
│   └── tests/
├── modes/                       sprechzimmer.yaml kanzlei.yaml verhandlungsraum.yaml
├── prompts/                     translate.md backtranslate.md summary-*.md repeat-back.md explain-simply.md
├── glossaries/                  medical-de.yaml legal-de.yaml deal-de.yaml
├── profiles/                    spark.yaml gpu-24.yaml mac.yaml edge.yaml lite.yaml
├── app/                         React + Vite + TypeScript PWA
│   └── src/screens/             Join Talk Summary Table Admin
├── deploy/                      docker-compose.yml Caddyfile livekit.yaml raum.nft systemd/
├── eval/                        sets/*.jsonl run_eval.py bakeoff.py load_test.py report.py
├── desktop/                     Tauri wrapper (P2)
├── landing/                     static landing page (Persuade)
└── internal/                    jury notes, never committed (.gitignore)
```

**Stack.** Python 3.12 with `uv`; FastAPI; `livekit`, `livekit-api`, `livekit-agents`, `livekit-plugins-silero`; `httpx`; `numpy`, `soundfile`, `soxr` (resampling); `onnxruntime`; DeepFilterNet ONNX (`deepfilter-stream`, verify on ARM, fallback to the `deepfilternet` package inside an NGC PyTorch container); `cryptography`; `dateparser`; `pydantic`; `pyyaml`. Frontend: React, Vite, TypeScript, `livekit-client`, `@livekit/components-react`, `qrcode`. Infra: Docker, Caddy, nftables, systemd, Tailscale.

**Git workflow.** `main` always demoable. Short feature branches `feat/<id>-<name>` merged often. Tags: `v0.1-first-turn` (G2), `v0.5-checks` (G3), `v0.9-freeze` (G5), `v1.0-submission` (G6). Commit timestamps are our proof that it was built at the event. **Never commit `internal/`, models, recordings or `.env`.**

---

## 8. Spark runbook

Every step ends with a check. Claude Code runs steps S3 onwards over SSH once S1–S2 are done. Placeholders: `<spark>` = hostname or IP, `<user>` = Spark login.

### S0 · Power and network (human)

1. Plug the original 240 W adapter into the Spark's power USB-C port (the only one marked for power). It starts immediately; there is no power button.
2. Connect the Spark by **Ethernet** to our travel router. Connect the router's WAN port to the venue internet for downloads.
3. Join this Mac to the same router Wi-Fi.
4. Find the Spark's IP in the router's client list. Set a **DHCP reservation** so the IP never changes, wherever the router goes.

Check (Mac): `ping -c 3 <spark>`

### S1 · SSH from the Mac (human, 2 minutes)

```bash
ssh <user>@<spark>                 # first login, accept fingerprint, enter password
exit
ssh-copy-id <user>@<spark>         # passwordless from now on
cat >> ~/.ssh/config <<'EOF'
Host spark
  HostName <spark-ip>
  User <user>
  ServerAliveInterval 30
EOF
ssh spark 'hostname; uname -m'     # expect: aarch64
```

NVIDIA Sync (desktop app) works too, but Claude Code needs plain SSH.

### S2 · Temporary passwordless sudo (human, revert after the hackathon)

Claude Code cannot type a sudo password. For the two days only:

```bash
ssh -t spark 'echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/99-raum-hackathon && sudo chmod 440 /etc/sudoers.d/99-raum-hackathon'
```

Revert on Wednesday: `ssh -t spark 'sudo rm /etc/sudoers.d/99-raum-hackathon'`

### S3 · System check (Claude Code)

```bash
ssh spark 'nvidia-smi; df -h /; free -g; docker --version; python3 --version; cat /etc/os-release | head -3'
ssh spark 'docker run --rm --gpus all ubuntu:24.04 nvidia-smi'   # GPU visible inside containers
```

Needs: ≥ 150 GB free disk, GPU visible in Docker.

### S4 · Ollama and models (Claude Code; downloads ~90 GB, start immediately)

```bash
ssh spark 'curl -fsSL https://ollama.com/install.sh | sh'
ssh spark 'sudo mkdir -p /etc/systemd/system/ollama.service.d && sudo tee /etc/systemd/system/ollama.service.d/raum.conf >/dev/null <<EOF
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_MAX_LOADED_MODELS=3"
Environment="OLLAMA_NUM_PARALLEL=4"
Environment="OLLAMA_HOST=127.0.0.1:11434"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama'
ssh spark 'ollama pull gemma4:26b && ollama pull bge-m3 && ollama pull gpt-oss:120b'
# bake-off only:
ssh spark 'ollama pull gemma4:e4b && ollama pull qwen3:30b-a3b'
```

Check:

```bash
ssh spark "curl -s localhost:11434/api/chat -d '{\"model\":\"gemma4:26b\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Translate into Arabic. Output only the translation: Take 500 mg twice a day.\"}]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"message\"][\"content\"])'"
ssh spark 'ollama ps'      # PROCESSOR column must show 100% GPU
```

If `ollama ps` shows a CPU/GPU split, stop any vLLM container that touched the GPU and restart Ollama (reported Spark issue).

### S5 · Whisper (Claude Code, ~20 min)

```bash
ssh spark 'sudo apt-get update && sudo apt-get install -y build-essential cmake git ffmpeg'
ssh spark 'git clone https://github.com/ggml-org/whisper.cpp ~/whisper.cpp && cd ~/whisper.cpp && cmake -B build -DGGML_CUDA=1 && cmake --build build -j --config Release'
# if the CUDA arch is rejected: add -DCMAKE_CUDA_ARCHITECTURES=native
ssh spark 'cd ~/whisper.cpp && ./models/download-ggml-model.sh large-v3-turbo'
```

Run as a service (`deploy/systemd/whisper.service`):

```ini
[Service]
User=raum
ExecStart=/home/<user>/whisper.cpp/build/bin/whisper-server -m /home/<user>/whisper.cpp/models/ggml-large-v3-turbo.bin --host 127.0.0.1 --port 8081 -t 8
Restart=always
```

Check: `ssh spark 'curl -s 127.0.0.1:8081/inference -F file=@/home/<user>/whisper.cpp/samples/jfk.wav -F response_format=json'`

### S6 · Python environment (Claude Code)

```bash
ssh spark 'sudo useradd -r -m -d /opt/raum raum || true; curl -LsSf https://astral.sh/uv/install.sh | sh'
ssh spark 'cd /opt/raum && git clone https://github.com/TheWingTheWong/raum.git app && cd app/core && ~/.local/bin/uv sync'
```

The repo is private: the Spark needs a deploy key or a GitHub token (see §18). Check: `uv run python -c "import livekit, onnxruntime, cryptography; print('ok')"`

### S7 · LiveKit server (Claude Code)

```bash
ssh spark 'docker run -d --name livekit --restart unless-stopped --network host livekit/livekit-server --dev --bind 0.0.0.0 --node-ip <spark-ip>'
```

`--dev` uses key `devkey` / secret `secret` (fine on our private network; real keys in `deploy/livekit.yaml` for anything else). Check: `curl -s http://<spark>:7880` returns OK.

### S8 · Voice output (Claude Code)

1. **Piper first (CPU, minutes):** `uv add piper-tts`; download one voice per demo language from the Piper voices repository; **check each voice's licence**.
2. **Chatterbox (time-boxed 45 min, Sprint 3):** inside an NGC PyTorch container (`nvcr.io/nvidia/pytorch:<current>-py3`), install `chatterbox-tts`, expose `:8082`. If it fails, Piper stays.

### S9 · Caddy and HTTPS (Claude Code)

`deploy/Caddyfile` (local CA variant):

```
<spark-ip> {
  tls internal
  encode zstd gzip
  handle /api/* { reverse_proxy 127.0.0.1:8000 }
  handle { root * /opt/raum/app/app/dist; try_files {path} /index.html; file_server }
}
<spark-ip>:7443 {
  tls internal
  reverse_proxy 127.0.0.1:7880
}
```

Root certificate for team phones: `/data/caddy/pki/authorities/local/root.crt` in the container (or `~caddy/.local/share/caddy/pki/authorities/local/root.crt` with the apt package). Install on each team phone (§9.3).

### S10 · Tailscale (Claude Code + one human login)

```bash
ssh spark 'curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up --ssh'
# prints a login URL → a human opens it and approves (§18)
```

In the Tailscale admin console (human): enable **MagicDNS** and **HTTPS certificates**. Then `ssh spark 'sudo tailscale cert <spark-name>.<tailnet>.ts.net'` gives a publicly trusted certificate (see §9.3 option A).

### S11 · Firewall counters and sealed mode (Claude Code)

`deploy/raum.nft` implements §6.11. Load with `sudo nft -f deploy/raum.nft`; check with `sudo nft list counters table inet raum`.

### S12 · RAUM services (Claude Code)

`deploy/systemd/raum-core.service` and `raum-agent.service` (user `raum`, `Restart=always`), plus `docker compose up -d` for LiveKit and Caddy. Check: open `https://<spark-ip>/admin` from the Mac.

### S13 · Moving the box (human, only if we change location)

Shut down the Spark from the OS, then move the Spark **and the router** together. With the same router and DHCP reservation, every IP, QR code and certificate keeps working.

---

## 9. Network, HTTPS and remote runbook

### 9.1 Router

- SSID `RAUM`, WPA2, 5 GHz preferred. **Client isolation off.**
- DHCP reservation for the Spark. Spark on Ethernet.
- WAN cable labelled **PULL ME** for the demo.
- Optional local DNS entry for the tailnet name (§9.3 A). GL.iNet routers support custom DNS entries; other brands vary.

### 9.2 QR codes

| QR | Content | Who |
|---|---|---|
| Wi-Fi | `WIFI:T:WPA;S:RAUM;P:<password>;;` | Everyone (iOS and Android cameras join directly) |
| Professional | `https://<host>/j/<room>?role=a` | Doctor / lawyer / CEO |
| Other party | `https://<host>/j/<room>?role=b` | Patient / client / partner |
| Jury | `http://<spark-ip>/l/<room>` (listen-only) | Jurors |

Print the Wi-Fi and jury QR side by side on slide 1 and on a table card.

### 9.3 HTTPS for microphones

Browsers only allow the microphone on HTTPS. Listen-only views (jury) do not need the microphone. **Verify in Sprint 1 that the listen-only view works over plain HTTP on iPhone and Android; if not, jurors follow on the table screen.**

| Option | Trusted on unprepared phones? | Works with the cable pulled? | Use for |
|---|---|---|---|
| **A. Tailnet certificate** (`tailscale cert`) served by Caddy on the LAN, plus a router DNS entry mapping the ts.net name to the Spark's LAN IP | Yes (publicly trusted) | Yes, if the router answers DNS | Best option for jurors and guests, free |
| B. Caddy local CA, root certificate installed on each phone | Only after install | Yes | Team phones |
| C. `tailscale serve` | Yes, tailnet devices only | Needs tailnet; local paths keep working briefly | Remote teammate |

Caveat for A: phones with Private DNS (Android) or iCloud Private Relay ignore the router's DNS. Ask jurors to use the jury QR; fall back to the table screen.

**Installing the local CA on team phones (option B):**
- iPhone: AirDrop or open `root.crt` → Settings → Profile Downloaded → Install → Settings → General → About → Certificate Trust Settings → enable.
- Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate.

### 9.4 Remote demo (A-04)

- Remote teammate's phone runs the Tailscale app, logged into our tailnet.
- They join `https://<spark-name>.<tailnet>.ts.net/j/<room>?role=a` over mobile data from outside the hall.
- Glass box shows their badge as **Tunnel** with latency.
- **Order on stage:** remote join first, then pull the cable. Remote drops gracefully; the room continues locally.

---

## 10. Schedule and gates

Roles. **A** = Spark & models (Claude Code + one human at the keyboard). **B** = App (Claude Code + frontend teammate). **D** = Design via impeccable (designer). **S** = Story, measurement, video. Fewer people: merge A+B and D+S.

### 10.1 Speed tactics (how we finish in ~13 hours)

1. **Never wait for downloads.** The ~90 GB of models download on the Spark during Sprint 0. Meanwhile Claude Code builds `core/` and `app/` on the Mac against a **mock pipeline** (canned transcripts and translations with realistic delays), so the UI is ready when the models are.
2. **Parallel Claude Code agents.** Up to four build agents work at once on separate modules: pipeline (agent), app (UI), security and privacy, evaluation. **The event protocol (§6.5) and room schema (§6.6) are the contract**, so modules plug together without waiting on each other. One integration owner (Claude Code main session) merges to `main`.
3. **One-command deploy.** `deploy/push.sh` rsyncs the repo to the Spark, rebuilds the app and restarts `raum-core` and `raum-agent`. Every change reaches the box in under 30 s.
4. **`main` is always demoable.** Merge small, merge often, tag at every gate.
5. **Test on real phones at every gate**, not only in the laptop browser.
6. **Cut early, not late** (cut rules below). A missed gate triggers its cut immediately.
7. **People rest in shifts.** Humans sleep 3–4 hours in turns while Claude Code keeps building and testing; nobody pitches without sleep.

### 10.2 Continuous sprint

Times assume T0 ≈ 13:00 Monday; shift everything by the real T0.

| Sprint | Clock (if T0 = 13:00) | A: Spark & models | B: App | D: Design | S: Story & measurement |
|---|---|---|---|---|---|
| **Sprint 0** · T0 → +1 h | 13:00–14:00 | S3–S5: system check, Ollama, **start all downloads**, Whisper build, LiveKit | Scaffold `core/` + `app/`; QR join, consent, mic publish; **mock pipeline** on the Mac; `deploy/push.sh` | `/impeccable init` → PRODUCT.md; `/impeccable shape talk` | Pick demo language + native speaker; write 30 test sentences (§12); line up 3 interviews |
| **G1** | 14:00 | | | | |
| **Sprint 1** · +1 → +3 h | 14:00–16:00 | Agent pipeline on the Spark: DeepFilterNet → VAD → Whisper → Gemma streaming; **model bake-off** | Talk screen with partials and streaming translation; table screen layout | Visual world → DESIGN.md; room icons and accents | Hall recordings with reference text; interviews and quotes; HTTP listen-only test on iPhone + Android |
| **G2: first full turn** · tag `v0.1-first-turn` | 16:00 | | | | |
| **Sprint 2** · +3 → +5 h | 16:00–18:00 | Fact guard; back-translation + bge-m3; uncertainty flag; timing events | Badges, red confirm, glass box, off-the-record, reconnect | `/impeccable onboard join`; first-run and consent copy | First latency table; test-set translations approved by native speaker |
| **G3** · tag `v0.5-checks` | 18:00 | | | | |
| *Move if the venue closes (18:00–18:45): Spark + router travel together; IPs stay the same (S13)* | | | | | |
| **Sprint 3** · +5 → +8 h | 18:45–21:00 | Encryption, burn, audit log, privacy meter, seal; Tailscale + connection badge; Piper voice + echo pause (Chatterbox time-box 45 min) | Sprechzimmer complete; summary + take-home QR; burn animation | Landing page | Calibrate check thresholds; `run_eval.py`; slides v1 |
| **G4** | 21:00 | | | | |
| **Sprint 4** · +8 → +11 h | 21:00–00:00 | Kanzlei + Verhandlungsraum configs; offer tracker; document camera; load test; `install.sh` (spark + lite) | Mode switch; explain-simply approval; admin console; document camera UI | Room panels | Proof numbers; video storyboard and shots |
| **G5: feature freeze** · tag `v0.9-freeze` | 00:00 | | | | |
| **Sprint 5** · +11 → +13 h | 00:00–02:00 | Bug fixes only; demo data; warm-up script | Bug fixes only | `/impeccable critique` + `audit` (one batched round), then `/impeccable polish` | **Backup demo video**; edit 60–90 s video; slides final |
| **G6: code freeze, product ready** · tag `v1.0-submission` | 02:00 | | | | |
| **Rest** | 02:00–07:00 | Sleep in shifts; Claude Code runs the load test and eval once more and writes a report | | | |
| **Pitch prep** | 07:00–pitch | Table setup (§17); 5+ timed rehearsals; Q&A drill (§15); fallback drills (§13) | | | |

### 10.3 Cut rules

- **No G2 by T0 + 3 h:** drop C-13, F-03–F-11, A-03; protect core + verification.
- **No G3 by T0 + 5 h:** drop F-07 (document camera), K-02, N-02.
- **No G4 by T0 + 8 h:** Kanzlei and Verhandlungsraum ship as configs with guards only.
- **No G5 by T0 + 11 h:** freeze anyway; whatever isn't done goes on the roadmap slide.
- Voice output and apps come last. **A text-only RAUM that checks every sentence still wins.**

---

## 11. Task board

Owner in brackets. `→` = acceptance check. Claude Code executes most A and B tasks; humans review on real phones.

### Sprint 0 · T0 → +1 h
- [ ] A1 [human] S0–S2 → `ssh spark hostname` works without password
- [ ] A2 [Claude] S3 system check → GPU in Docker, disk ≥ 150 GB free
- [ ] A3 [Claude] S4 Ollama + start downloads → `ollama list` shows models
- [ ] A4 [Claude] S5 Whisper build → `/inference` returns JFK text
- [ ] B1 [Claude] Repo scaffold (`core/`, `app/`, `modes/`, `deploy/`, `eval/`) → CI-free, runs locally
- [ ] B2 [Claude] `POST /api/join` issues LiveKit token with role/lang metadata → phone joins room
- [ ] B3 [Claude] Join, consent, Talk shell with mic publish → audio track visible in LiveKit
- [ ] D1 [designer] `/impeccable init`, `shape talk` → PRODUCT.md committed
- [ ] S1 [story] Demo language and native speaker chosen; 30 test sentences in `eval/sets/` (§12)

### Sprint 1 · +1 → +3 h
- [ ] A5 [Claude] Agent: per-track 48 kHz stream → DeepFilterNet → Silero VAD → buffer → Whisper final → `transcript.final`
- [ ] A6 [Claude] Partials every ~1 s at background priority → `transcript.partial`
- [ ] A7 [Claude] Gemma streaming translation per target language → `translation.delta/final`
- [ ] A8 [Claude] `eval/bakeoff.py` → table of quality and latency per candidate; pick winner
- [ ] B4 [Claude] Talk screen: partial captions, streamed translation, waveform → works on 2 phones
- [ ] B5 [Claude] Table screen skeleton `/t/:room` → both languages side by side
- [ ] D2 [designer] DESIGN.md tokens and room accents → applied to Talk screen
- [ ] S2 [story] Hall recordings with reference transcripts; 3 interviews → quotes in `internal/`

### Sprint 2 · +3 → +5 h
- [ ] A9 [Claude] `verify/guard.py` + unit tests → all perturbed numbers caught
- [ ] A10 [Claude] Back-translation + bge-m3 → `verify.result` badges
- [ ] A11 [Claude] Uncertainty flag from ASR confidence → "please say that again"
- [ ] A12 [Claude] `pipeline.timing` events → glass box numbers
- [ ] B6 [Claude] Badges, red confirm prompt, off-the-record, reconnect banner
- [ ] B7 [Claude] Glass box animation + GPU stats
- [ ] D3 [designer] `/impeccable onboard join` → first-run copy final
- [ ] S3 [story] First latency table; HTTP listen-only test on iPhone and Android

### Sprint 3 · +5 → +8 h
- [ ] A13 [Claude] `security/crypto.py`, `burn.py`, `audit.py` + `raum audit verify`
- [ ] A14 [Claude] `deploy/raum.nft` + `privacy/meter.py` + seal toggle
- [ ] A15 [Claude] S10 Tailscale + connection badge (G-03)
- [ ] A16 [Claude] S8 Piper voice output + echo pause; Chatterbox time-box 45 min
- [ ] B8 [Claude] Sprechzimmer: dose/allergy confirm, repeat-back, take-home QR, file note, human-interpreter button
- [ ] B9 [Claude] Summary screen + approve + print-to-PDF stylesheet; burn animation
- [ ] D4 [designer] Landing page
- [ ] S4 [story] Threshold calibration; `eval/run_eval.py` report; slides v1; video storyboard

### Sprint 4–5 · +8 → +13 h
- [ ] A17 [Claude] `modes/kanzlei.yaml`, `modes/verhandlungsraum.yaml` + offer tracker extraction
- [ ] A18 [Claude] Document camera via Gemma 4 vision (F-07)
- [ ] A19 [Claude] `eval/load_test.py` across 1/2/4/8/12 rooms → capacity chart
- [ ] A20 [Claude] `install.sh` with `spark` and `lite` profiles; test `lite` on the M2 Mac
- [ ] B10 [Claude] Mode switch, explain-simply approval, offer tracker UI, admin console
- [ ] D5 [designer] Room panels; `/impeccable critique` + `audit` + `polish`
- [ ] S5 [story] Proof slide numbers; record backup demo video; final video edit

---

## 12. Evaluation and measurement

### 12.1 Test set format (`eval/sets/*.jsonl`)

```json
{"id":"sz-03","room":"sprechzimmer","src_lang":"de","tgt_lang":"ar",
 "src":"Nehmen Sie zweimal täglich 500 mg Metformin.",
 "facts":[{"kind":"dose","value":"500 mg"},{"kind":"frequency","value":"2/day"}],
 "perturbed":{"tgt_text":"<translation with 50 mg>","expect":"red"}}
```

- 30 sentences: 10 per room, both directions, every one with at least one fact.
- 10 perturbed translations for the checker: wrong number (3), wrong unit (2), negation flipped (2), allergy omitted (2), date shifted (1).
- 10 clean translations (native-speaker approved) to measure false alarms.

### 12.2 Metrics for the proof slide

| Metric | How | Target |
|---|---|---|
| First words on screen after speaker stops | `pipeline.timing`, p50 and p95 over a 5-minute session | p50 < 1.0 s |
| Full sentence | same | p50 < 1.5 s |
| QR scan to first translated sentence | stopwatch, 3 runs | < 10 s |
| Transcription error rate in the hall, with and without DeepFilterNet | `eval/run_eval.py` with hall recordings | lower with cleaning |
| Fact guard on perturbed numbers | unit tests + eval | 100% caught |
| Checker on semantic perturbations | eval | ≥ 80% caught |
| False alarms on clean set | eval | < 10% |
| Native-speaker rating | 1–5 on 20 translations | ≥ 4 |
| Conversations at once with p95 < 3 s | `eval/load_test.py` | measured value |
| Bytes to third parties | privacy meter | 0 |

### 12.3 Load test

`eval/load_test.py` creates N rooms, each with two bot participants (LiveKit Python SDK) that publish pre-recorded utterances every 12 s for 5 minutes. It collects `pipeline.timing` events and reports p50/p95 per N in {1, 2, 4, 8, 12}. Output: `eval/out/load.csv` and a chart for slide 4.

---

## 13. Demo script

**Cast.** P = presenter. J = a juror who plays the doctor (ask Hanke or Wachs; fallback: a teammate). A = teammate speaking the demo language as patient. R = remote teammate outside the hall on mobile data. O = operator at the laptop.

**Before the slot.** Router on, cable in, Spark on Ethernet, models warm, two phones joined, table screen at `/t/<room>`, jury QR on slide 1, backup video queued, sealed mode off.

| Step | Action | Line | Fallback |
|---|---|---|---|
| 0 | Slide 1 shows Wi-Fi + jury QR | "Scan this. You'll follow along in your language." | Jurors watch the table screen |
| 1 | J gets the doctor phone (German), A the patient phone | "You are the doctor. Just talk." | P plays doctor |
| 2 | A: "I take 500 mg metformin twice a day, and I'm allergic to penicillin." | | Pre-joined second phone |
| 3 | Dose and allergy light up; J taps confirm; badge green; glass box shows timings | "Code checked the numbers. A second model checked the meaning." | Point at the glass box screenshot slide |
| 4 | (If built) A photographs the demo doctor's letter; explanation appears in A's language | "Paper too." | Skip |
| 5 | R joins from outside; badge "Tunnel" | "Works from anywhere." | Skip if tunnel down |
| 6 | O pulls the WAN cable; conversation continues; R shows offline banner; meter stays 0 | "Works with nothing." | Meter screenshot |
| 7 | O switches to Kanzlei: a deadline flagged (10 s) | | Slide |
| 8 | O switches to Verhandlungsraum: three offers in the tracker (10 s) | | Slide |
| 9 | J taps End; summary in both languages; A's phone gets the take-home | "She leaves with something." | Pre-generated summary |
| 10 | Burn; "key destroyed" in the audit log | "Translates. Checks. Forgets." | Backup video |

---

## 14. Pitch, slides and video

### 14.1 Three-minute script (English)

| Time | Words (approximate) |
|---|---|
| 0:00 | "Scan the code on screen. In a moment you'll hear this pitch in your own language." |
| 0:10 | "Amira is at the doctor. Her 12-year-old son translates her diagnosis. He doesn't know the words. She doesn't know what he left out." |
| 0:25 | "Insurers don't pay for interpreters. And doctors, lawyers and companies can't send these conversations to a cloud: for doctors and lawyers that's a crime under §203." |
| 0:35 | "This is RAUM. Any AI can translate. RAUM checks." |
| 0:40 | Demo steps 1–3 with the juror as doctor |
| 1:30 | Demo step 4 (document) if built |
| 1:40 | Demo steps 5–6: "Works from anywhere. Works with nothing." |
| 2:00 | Demo steps 7–8: "Same box, other rooms." |
| 2:15 | "We measured it: [first words in X s], [Y% of wrong numbers caught], [Z conversations at once], zero bytes to third parties. Where the AI is unsure, a human decides." |
| 2:35 | "Clinics, law firms and exporters. The framework is open; the certified box and professional rooms are paid. Like Nextcloud, built in Berlin." |
| 2:50 | Demo steps 9–10. "RAUM. Translates. Checks. Forgets." |

### 14.2 Six slides

1. **Problem:** Amira; three facts; Wi-Fi + jury QR.
2. **RAUM:** the slogan; three rooms with icons.
3. **How it works:** glass-box diagram; seven models; "nothing leaves the box".
4. **Proof:** measured numbers (§12.2), bake-off table, licences.
5. **Human in control:** what the AI does, what the human decides, where it fails; the "not built" list.
6. **Business and roadmap:** open core, customers, box per room; NetBird, guest links, iOS app.

### 14.3 Video (60–90 s, doubles as backup)

1. Close-up: a child translating for a parent (staged, team member).
2. Title: "Translates. Checks. Forgets."
3. QR scan → first sentence → green tick (screen recording + phone shot).
4. Glass box timings.
5. Cable pulled, meter at 0.
6. Summary on the patient's phone, burn, logo.

---

## 15. Q&A bank

| Question | Answer |
|---|---|
| Why not DeepL or Apple? | Cloud or few languages, and neither checks its output. For doctors and lawyers, cloud translation is a legal risk. |
| Who is liable for a wrong translation? | The professional decides. Code checks critical facts, unclear sentences are flagged, and a human interpreter is one tap away. |
| Is it a medical device? | No. It's a communication aid and makes no diagnosis. |
| How accurate is it? | We name the languages we measured and show the numbers. That's why the check exists. |
| Why Gemma 4 and gpt-oss? | Gemma 4 translates fast and is Apache 2.0. The checker is a different model family, so they don't share blind spots. We chose by measurement (bake-off). |
| Why does it need a Spark? | All models stay loaded at once: about 90 GB. A laptop runs one small model. |
| What does it cost? | The Spark is the pilot. The product is a box per room, paid monthly. |
| GDPR? | Local, encrypted, burned by key deletion, logged without content, no third-party processing. |
| What about remote use? | Encrypted tunnel to your own box. The meter separates third-party bytes (0) from tunnel bytes. |
| Isn't Tailscale a US company? | The tunnel content is end-to-end encrypted. For full sovereignty: self-hosted NetBird, built in Berlin. |
| Can others use it? | One install command, hardware profiles, rooms as files, Apache 2.0. |
| What if the internet fails? | We just showed it: nothing changes locally. |
| Rare languages? | Whisper is weaker for some; the uncertainty flag and checks catch it; the human-interpreter button exists. |
| What did you build here vs. before? | Everything in the repo, with commit history from these two days. |
| Next step? | Pilot with one Berlin practice and one law firm; measure time saved on documentation. |

---

## 16. Risk register

| Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|
| Model downloads too slow on venue Wi-Fi | High | High | Start in Sprint 0; Ethernet; pull 120b last; fallback gpt-oss:20b | A |
| Python packages missing on ARM64 | Medium | High | Native builds for Ollama/whisper.cpp; ONNX on CPU; NGC containers | A |
| LiveKit integration takes too long | Medium | High | Time-box 90 min; fallback to plain WebSocket audio | A/B |
| Phones refuse microphone (HTTPS) | High | High | Local CA on team phones; tailnet cert for others; test in Sprint 1 | A |
| Android can't resolve `.local` | High | Medium | Use IPs in QR codes | B |
| Latency above target | Medium | High | Streaming everywhere; `gemma4:e4b` fallback; vLLM later | A |
| Echo loops with speakers | Medium | Medium | Mic pause during playback; headphones | A |
| Chatterbox won't run on ARM | Medium | Low | Piper default; text-only is fine | A |
| Venue Wi-Fi interference | High | Medium | Own router, 5 GHz, Spark on Ethernet | human |
| Demo language quality weak | Medium | High | Bake-off; demo only in a fluently spoken language; rehearse sentences | S |
| Juror declines to play doctor | Low | Low | Teammate plays doctor | P |
| Spark overheats | Low | Medium | Free airflow, no bag during demo | human |
| Power at the table | Medium | High | Extension lead, power strip | human |
| Live demo fails | Medium | High | Backup video; screenshot slides; fallback per step (§13) | O |
| Juror phones ignore router DNS | Medium | Low | Listen-only over HTTP; table screen | B |
| Overclaiming | Low | High | Only measured numbers on slides; honesty rules | everyone |
| Team exhaustion overnight | High | High | Sleep in shifts after G6; Claude Code keeps building and testing; rehearse after rest | everyone |
| Venue closes mid-sprint | High | Medium | Move Spark + router together at a gate; same IPs (S13) | human |

---

## 17. Submission and packing checklists

### On the jury table
- [ ] Spark with RAUM sticker, on Ethernet, airflow free
- [ ] Router with WAN cable labelled **PULL ME**
- [ ] Two phones (one Android, one iPhone), charged
- [ ] Laptop showing the table screen; HDMI if a monitor is offered
- [ ] Two pairs of wired headphones (or low-latency)
- [ ] Optional USB speakerphone
- [ ] Table card with Wi-Fi + jury QR
- [ ] Demo doctor's letter (fictional, printed) for the document camera

### Digital
- [ ] Six slides + backup screenshot slides
- [ ] 60–90 s video, also offline on the laptop
- [ ] Landing page with QR
- [ ] README with architecture, licences, how to run
- [ ] Tag `v1.0-submission`
- [ ] Before making the repo public: confirm `internal/` was never committed; remove anything sensitive

### Packing list
- [ ] Spark + original 240 W adapter
- [ ] Router + its power supply + 2 Ethernet cables
- [ ] Extension lead / power strip; plug adapter if needed
- [ ] Phones + chargers; headphones; USB-C cables
- [ ] Printed QR cards; demo letter
- [ ] Laptop + charger

---

## 18. What the humans must do

These are the only steps Claude Code cannot do. Everything else runs over SSH.

### Right now (this starts T0)
1. **Hardware:** Spark on its 240 W adapter, Ethernet to our router, router WAN to the venue internet, this Mac on the router Wi-Fi (§8 S0).
2. **Tell Claude Code:** the Spark's IP, its username, and that S1 is done:
   ```bash
   ssh-copy-id <user>@<spark-ip>
   ```
3. **Passwordless sudo for two days** (§8 S2):
   ```bash
   ssh -t <user>@<spark-ip> 'echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/99-raum-hackathon && sudo chmod 440 /etc/sudoers.d/99-raum-hackathon'
   ```
4. **Choose the demo language** and the teammate who speaks it fluently.
5. **GitHub usernames** of teammates, so Claude Code can add them as collaborators.

### During Sprint 0–1
6. **Repo access for the Spark** (private repo): either approve a deploy key Claude Code generates, or run `gh auth login` on the Spark once.
7. **Tailscale login** (when Claude Code shows a login URL): open it, sign in, approve the Spark and the remote teammate's phone. In the admin console enable MagicDNS and HTTPS certificates.
8. **Phones:** install the local CA certificate on team phones (§9.3 B); install the Tailscale app on the remote teammate's phone.
9. **Interviews:** three students (and, if possible, one practice or lawyer by phone) for quotes and "would use again".

### Before the pitch
10. Confirm pitch time and length; who presents; who operates.
11. Ask a juror (Hanke or Wachs) at the start of the slot to play the doctor.
12. Pack and set up the table (§17).

---

## 19. Open decisions

| Decision | Default if nobody decides |
|---|---|
| Demo language | The language our fluent teammate speaks best (Russian or Turkish work well with Whisper) |
| Team roles | A + B: Claude Code with you; D: designer; S: whoever is left |
| Pitch time and length | Assumed 3 minutes on Tuesday; the sprint finishes ~13 h after T0 regardless |
| Repo visibility | Private now; public at submission after the check in §17 |
| Translator | Bake-off winner; default `gemma4:26b` |
| Voice output | Piper; Chatterbox only if it runs by G4 |

---

## 20. Appendix: prompts, schemas, test data

### 20.1 Translation prompt (`prompts/translate.md`, Gemma 4)

```
You are the interpreter in a {room_name} conversation between {role_a} ({lang_a}) and {role_b} ({lang_b}).
Translate the next utterance from {src_lang} into {tgt_lang}.
Rules:
- Translate faithfully and completely. Do not add, omit, explain, summarise or answer.
- Keep every number, unit, date, time, name and medication name exactly as said.
- Register: {register}.
- Use these fixed translations: {glossary}
- Earlier turns, for names and pronouns only (do not translate them): {context}
Output only the translation.
```

### 20.2 Back-translation prompt (`prompts/backtranslate.md`, gpt-oss)

```
Reasoning: low
Translate the following {tgt_lang} text into {src_lang}. Be literal and complete. Keep numbers and units exactly.
Output only the translation.
```

### 20.3 Repeat-back check (`prompts/repeat-back.md`, gpt-oss)

```
Reasoning: low
Instruction given by the doctor ({lang_a}): {instruction}
Patient's restatement ({lang_b}): {restatement}
Return JSON: {"understood": "yes|partly|no", "missing": ["..."], "wrong": ["..."]}
Compare meaning, doses, frequencies and warnings. Be strict about numbers.
```

### 20.4 Summary templates (gpt-oss, JSON out, rendered bilingually)

- **Sprechzimmer:** `medications[{name, dose, frequency, duration}]`, `allergies[]`, `next_appointment`, `warning_signs[]`, `open_questions[]`, `file_note_de`.
- **Kanzlei:** `parties[]`, `facts[]`, `dates[]`, `deadlines[{what, date, consequence}]`, `documents_requested[]`, `next_steps[]`, `case_note_de`.
- **Verhandlungsraum:** `agreed[]`, `open[]`, `offers[{amount, currency, by, time}]`, `next_steps[{what, who, when}]`.

Rule for all: only facts stated in the transcript; mark anything uncertain; no advice.

### 20.5 Explain simply (`prompts/explain-simply.md`)

```
Reasoning: low
Rewrite this legal statement for a client with no legal training, in {lang_b}, at plain-language level (A2–B1).
Keep every date, deadline and amount. Do not give advice beyond what is stated.
Statement ({lang_a}): {text}
```

### 20.6 Glossary seed (`glossaries/medical-de.yaml`)

```yaml
terms:
  - de: Metformin        # keep as is in all languages
  - de: Penicillin
  - de: Blutdruck        # en: blood pressure
  - de: nüchtern         # en: on an empty stomach
  - de: zweimal täglich  # en: twice a day
  - de: Überweisung      # en: referral
  - de: Rezept           # en: prescription
```

### 20.7 Demo sentences (translate into the demo language with the native speaker)

- Sprechzimmer: "Ich nehme zweimal täglich 500 mg Metformin." / "Ich bin allergisch gegen Penicillin." / "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder."
- Kanzlei: "Die Klagefrist endet am 24. November." / "Bringen Sie bitte Ihren Arbeitsvertrag und die letzten drei Lohnabrechnungen mit."
- Verhandlungsraum: "Unser Angebot liegt bei 1,2 Millionen Euro." / "Wir können auf 1,05 Millionen gehen, wenn die Lieferung bis März erfolgt."

### 20.8 Sources

Product facts and jury research: see the brief. Model facts: [Gemma 4 model card](https://ai.google.dev/gemma/docs/core/model_card_4), [Gemma 4 announcement](https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/), [Ollama gemma4 tags](https://ollama.com/library/gemma4), [TranslateGemma](https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/), [Gemma 4 26B on DGX Spark](https://ai-muninn.com/en/blog/dgx-spark-gemma4-26b-nvfp4-52-toks), [NVIDIA forum: Gemma 4 on DGX Spark](https://forums.developer.nvidia.com/t/gemma-4-day-1-inference-on-nvidia-dgx-spark-preliminary-benchmarks/365503), [Tailscale Funnel](https://tailscale.com/kb/1223/tailscale-funnel), [DGX Spark hardware](https://docs.nvidia.com/dgx/dgx-spark/hardware.html).
