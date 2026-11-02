# Product

<!-- impeccable:product-schema 1 -->

> Source: `plan.md` §1–§3, §5, §13 and §19. The team directed init to derive this record from the plan instead of running an interview round; facts below are the plan's, open items are marked **Open**.

## Platform

web

(Progressive web app in the phone's browser for Join, Talk and Summary; laptop or monitor browser for the Table screen and Admin. No native app for the hackathon.)

## Users

- **The professional (role a)** — a doctor or clinic, a lawyer, or a company CEO. German-speaking, on their own phone or a shared tablet, mid-consultation, with no time to configure anything. Job: understand the other person exactly, catch the facts that matter (dose, allergy, deadline, price), and leave with the paperwork done.
- **The other party (role b)** — a patient, a client, or a foreign business partner who does not share the professional's language. Possibly older, anxious, on an unfamiliar phone, sometimes reading a right-to-left script. Job: be understood, understand every instruction, and take home something in their own language.
- **Onlookers** — jurors or colleagues following the conversation on the shared Table screen (bilingual conversation plus the glass box), read from about 3 metres away.
- **The operator** — whoever set up the box; uses Admin on a laptop (devices, rooms, GPU, latency, privacy meter, seal, burn-all).

Opening story (not a statistic): Amira is at the doctor; her 12-year-old son translates her diagnosis. He doesn't know the words, and she doesn't know what he left out.

## Product Purpose

RAUM (German for "room") is a private AI interpreter that lives in the room. It cleans each voice, transcribes it, translates both ways, checks every translation with code and with a second model, speaks it, and writes a bilingual summary at the end. Then it burns the session. Every model runs locally on one box; nothing leaves it.

Success: a person scans a QR code and sees their first translated sentence in under 10 seconds; first translated words appear under 1 second after the speaker stops; every number, unit, date and name is checked; the conversation ends on a summary both sides can keep; the session is provably destroyed.

## Positioning

- *"Any AI can translate. RAUM checks."* Code compares every number, unit, date, name and medication between source and translation; a second model from a different family back-translates and scores meaning; a human confirms what matters.
- *"Seven models, one box, zero bytes to third parties."* For doctors and lawyers, sending these conversations to a cloud is a legal risk (§203 StGB, §43e BRAO; GeschGehG §2 for trade secrets).
- *"Works from anywhere. Works with nothing."* Pull the internet cable and it keeps working.
- On stage it is a product (Amira, the box, three rooms). Underneath it is an open framework: any device, any hardware, rooms as plugin files.

## Operating Context

Three rooms where a leak is a legal problem, each one YAML file in `modes/`:

| Room | People | Signature |
|---|---|---|
| Sprechzimmer (leads the pitch) | Doctor and patient | Dose and allergy guard with confirmation, repeat-back check, take-home summary, German file note, "request a human interpreter" |
| Kanzlei | Lawyer and client | Deadline and date guard, "explain simply" approved by the lawyer, German case notes |
| Verhandlungsraum | CEO and foreign partner | Offer tracker, deal glossary, minutes both sides approve |

- Everyone joins with their own device by scanning a QR code; the QR encodes room and role. Language comes from the phone's settings.
- Journey: discover → set up (plug in, scan sticker) → join (one tap) → first words (under 1 s, green tick) → during (flags only when they matter) → end (summary in seconds) → close (burn, "key destroyed") → return (presets and glossary waiting).
- Habit loop: trigger (a person who doesn't speak German) → one action (scan, Start) → reward (understood, verified, paperwork done) → investment (glossary and presets grow).
- Live demo setting: a hackathon jury table with a box, a travel router, two phones (one Android, one iPhone), a laptop showing the Table screen, headphones; a cable labelled PULL ME.

## Capabilities and Constraints

- Surfaces (rule 8, three screens only on phones): **Join** `/j/:room?role=a|b`, **Talk** `/r/:room`, **Summary** `/r/:room/summary`; on the laptop **Table screen** `/t/:room` and **Admin** `/admin`; a static landing page `/`.
- Live feedback, never a dead second: waveform while listening, partial captions while speaking, translation typing in, a tick when verified.
- Verification states: green (verified), amber (unsure, please confirm, does not interrupt), red (mismatch, interrupts with a confirm prompt showing the exact pair). Checks never block display. Grey "check pending" while the checker runs.
- Off-the-record button (pipeline drops audio; both phones grey). Consent screen before joining: "This is an AI interpreter" (EU AI Act Art. 50), recording off.
- Glass box: each sentence moves through stages heard, cleaned, transcribed, translated, checked, spoken, with timings; model names, GPU load, connection badge per device (Direct, Tunnel, Relayed with ms), latency p50/p95.
- Privacy meter: "Bytes to third parties: 0" and "Encrypted tunnel to your own devices: X KB"; sealed mode.
- Burn by key deletion is the one signature moment: key overwritten, files deleted, "key destroyed" in the audit log.
- Speed targets: first translated words < 1.0 s after the speaker stops; full sentence < 1.5 s; voice < 1.5 s; check badge about +1 s.
- Non-goals: no lie detection, no emotion analysis, no hidden tactics advice for one side; not a medical device (communication aid, no diagnosis); no engagement tricks (no streaks, no nagging notifications); no accounts.
- **Open:** demo language (default Russian or Turkish; Arabic is plausible for the patient story). The UI must handle any language, including right-to-left scripts.

## Brand Commitments

- Name **RAUM**. Slogan **Translates. Checks. Forgets.** Tagline *What's said in the room stays in the room.*
- One-liner: A private AI interpreter that lives in the room. It cleans the audio, translates both ways, checks every translation and writes the minutes. Nothing leaves the box.
- Room names stay German (Sprechzimmer, Kanzlei, Verhandlungsraum) with translated labels. Each room gets an icon and an accent within one shared identity (app, table screen, slides, video, landing page).
- Hardware is named "NVIDIA GB10 box" or "ASUS Ascent GX10" in public copy, never "DGX Spark".
- Signature tick sound when a sentence is verified; vibration on Android only.

## Evidence on Hand

- Verified problem facts (sources in the product brief): statutory health insurance does not pay for interpreters; only 14 of 38 German university hospitals can get an interpreter at weekends; about 15% of people in Germany mainly speak another language at home; Berlin's medical association demanded insurer coverage in December 2025; §203 StGB, §43e BRAO, GeschGehG §2.
- Measured numbers (latency, catch rates, capacity) do not exist yet; they come from `eval/` during the event. Never show invented numbers, testimonials or customers.

## Product Principles

1. **One scan, zero decisions.** No account, language from the phone, room preset by the box, role from the QR. Every extra choice is a failure.
2. **Trust you can see.** Verification, privacy and destruction are shown, not claimed: badges, the zero-bytes meter, the visible burn.
3. **Humans decide.** The AI flags; the professional confirms. Only what matters interrupts.
4. **The other person's words are the hero.** On each phone, what the other person said, in your language, dominates.
5. **Leave with something.** Every conversation ends on the summary and a take-home copy.

## Accessibility & Inclusion

- Large readable type for older patients; high contrast in both light and dark themes; must work for people who are stressed, unwell or unfamiliar with the phone.
- Semantic colours for verified / unsure / mismatch are separate from the brand accent and never the only signal (icon and text too).
- Right-to-left layout for Arabic and other RTL scripts; any script must render.
- Motion is subtle and respects `prefers-reduced-motion`.
- Table screen glass box readable from 3 metres.
