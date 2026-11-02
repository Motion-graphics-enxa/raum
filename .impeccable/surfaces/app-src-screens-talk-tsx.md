---
version: 1
slug: "app-src-screens-talk-tsx"
primary_target: "app/src/screens/Talk.tsx"
related_targets: ["app/src/screens/Join.tsx","app/src/screens/Summary.tsx","app/src/screens/Table.tsx","app/src/screens/Admin.tsx"]
---

## Scope

Talk screen `/r/:room` (Operate, phone, portrait 360–430 px) and, sharing one world: Join `/j/:room`, Summary `/r/:room/summary`, Table screen `/t/:room` (laptop/monitor, read from 3 m), Admin `/admin`, listen-only `/l/:room`. Shape answers were derived from plan.md §3–§6 at the team's instruction (no interview round); assumptions are marked.

## Shape brief (Talk)

1. **Job and audience.** Two people across a desk, each on their own phone. The professional (German) and the other party (any language, possibly older, anxious, RTL). Mode: Operate. Each phone answers one question at a glance: what did the other person just say, in my language, and can I trust it?
2. **Outcome and proof.** The other person's latest sentence in my language is the hero, typing in as it streams, then a verified mark. Under it, every checked fact (dose, frequency, allergy, date, deadline, amount) is dimensioned as source value vs. translated value. Success: the listener reads it within a second of the speaker stopping and knows if it was checked.
3. **Selected direction.** The architect's floor plan (see contract). One dominant talk control at the bottom. Mismatch is the only interruption: a confirm sheet with the exact pair.
4. **Scope and boundaries.** Production-ready screens with real behaviour against the mock pipeline. No chat bubbles, no avatars, no streaks, no notification nagging. Not a medical device: no diagnostic language.
5. **States and ranges.** Sentences 2–40 words; facts 0–6 per sentence; scripts Latin, Cyrillic, Arabic (RTL). States: connecting, waiting for the other person, listening (my mic live), other person speaking (partial captions), translating (typing), checking (pending), verified, unsure, mismatch + confirm, uncertain ASR ("please say that again"), off the record (both phones grey), reconnecting, ended → summary, burned.
6. **Interaction and layout.** Top: title-block header (room, ids, languages, path). Middle: hero sentence, source line beneath in the speaker's script, dimensioned facts, then earlier turns stepping down a fixed recency ramp. Bottom: talk control (open mic with level meter; hold-to-talk optional) flanked by off-the-record and the room's buttons; End lives behind the title block menu. Confirm sheet slides from the bottom, two large answers.
7. **Constraints and open decisions.** Self-hosted fonts and no third-party requests (the privacy meter must stay at 0 and work with the cable pulled). WCAG AA in both themes; colour never the only state signal; prefers-reduced-motion. Demo language open: any language must work, RTL included.

## Direction contract

THESIS: RAUM draws the conversation the way an architect draws a room: every fact the box checked is dimensioned like a wall, and a mismatch is marked the way drawings mark a change, with a revision cloud. It refuses the category default of chat bubbles on clinical white with a teal accent and a glowing AI badge.

OWN-WORLD: Berliner Blau (Prussian blue, the blueprint pigment found in Berlin, 1706). Light theme is a white-print (Lichtpause): blue-black line work on drafting-film white, the shell header a solid Prussian band. Dark theme is the blueprint (Blaupause): pale cyan-white line work on a Prussian ground. Line work in DIN 1356 line types: solid, dashed, dotted, dash-dot. Architectural ticks (45° slashes) end every dimension. Title block cells (Schriftfeld) frame metadata. Atkinson Hyperlegible Next for words, Atkinson Hyperlegible Mono for every figure and label (hyperlegible digits: 500 never reads as 5OO), Noto Sans Arabic for Arabic. Semantic pens separate from the brand: verified green, unsure ochre, mismatch red. Each room is a hatch pattern plus one layer colour: Sprechzimmer teal, Kanzlei violet, Verhandlungsraum magenta.

STORY: The listener sees the other person's words appear in their own language, sees the numbers dimensioned and closed, and trusts it. When a number does not close, the cloud tells them exactly which pair and asks one question. At the end the sheet becomes the summary they keep; then the box erases the drawing and stamps the key destroyed.

FIRST VIEWPORT: Phone 390×844. Top 72 px: Prussian title-block band: room hatch swatch + name ("Sprechzimmer"), room id in mono, language pair "DE ⇄ AR", path cell ("Direkt 12 ms"), live dot. Centre (≈55% height): hero sentence at 30–34 px, left-aligned (right in RTL), typing in; beneath it the source sentence at 17 px in the speaker's script; beneath that a row of dimension annotations, each a short horizontal dimension line with ticks and the value written above ("500 mg", "2×/Tag"), state as line type. Above the hero, earlier turns step down the recency ramp and fade into the sheet. Bottom 168 px: the talk control, a 96 px circle in Prussian blue with the live level drawn as concentric contour lines, label under it; off-the-record left, the room's action right.

FORM: The architect's floor plan (Grundriss), candidate 7 of 7 on the ordered list (1 Medikationsplan, 2 BVG line strip, 3 Geprüft stamp, 4 Apotheke strength bands, 5 Braun appliance, 6 DIN enamel sign, 7 Grundriss). seed:22297e6653aee384. Raises, each named for its donor:
- From the lexicon (declined): colour is quarantined. State colour lives only inside the dimension or cloud that carries the fact, never as row fills or tinted backgrounds; a live correction is struck through and replaced in place.
- From the emission-line rail (declined): state is line form before hue. Solid = verified, dashed = checking, dotted = unsure, revision cloud = mismatch, grey hairline = off the record; every state reads in greyscale.
- From the star atlas (declined): one fixed recency ramp carries hierarchy. The newest sentence is largest; each older turn steps down the same ramp, nothing else decorates rank.
- From the cassette j-card (declined): running times sit tight against the margin. Every turn carries its measured time right-aligned against the 1.0 s budget; the glass box draws each turn's pipeline as a dimension chain (Maßkette) in milliseconds.
- From the monochrome product canon (declined): no claim without its proof. A verified mark always opens to the dimensioned pairs, the similarity score and the back-translation that earned it.
- From the character catalog (declined): every person has a fixed name plate. Role, language, device and connection path sit in a title-block cell that never moves.
Signature interaction: the dimension close. When verify.facts lands, each fact's dimension line draws from its first tick to its second (220 ms, ease-out) and the value settles above it; a mismatch draws a revision cloud around the pair instead. Signature moment: the burn, the sheet's line work retracts stroke by stroke, the title block stamps "Schlüssel vernichtet" with the time and audit hash.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
