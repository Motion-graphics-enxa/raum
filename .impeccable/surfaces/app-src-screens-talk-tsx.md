---
version: 1
slug: "app-src-screens-talk-tsx"
primary_target: "app/src/screens/Talk.tsx"
related_targets: ["app/src/screens/Join.tsx","app/src/screens/Summary.tsx","app/src/screens/Table.tsx","app/src/screens/Admin.tsx","app/src/screens/Home.tsx"]
---

## Scope

Talk screen `/r/:room` (Operate, phone, portrait 360–430 px) and, sharing one world: Join `/j/:room`, Summary `/r/:room/summary`, Table screen `/t/:room` (laptop/monitor, read from 3 m), Admin `/admin`, listen-only `/l/:room`, Home `/`. Full replacement of the Milchglas world, 2 Nov 2026: the user asked to "fully redo the UI, change everything: colours, the way things look, animation, professional"; frontend only, backend untouched. Kept: name, slogan, room names, English chrome with content in each person's language. Ruled out by the user: too dark and techy. All screens get equal care.

## Shape brief (Talk)

1. **Job and audience.** Two people across a desk, each on their own phone: the professional and the other party (any language, possibly older, anxious, RTL). Mode: Operate. Each phone answers at a glance: who is speaking, what did they just say in my language, is it draft or final, and can I trust it?
2. **Outcome and proof.** The other person's newest sentence in my language is the hero; while it streams it is a draft (in shade), then it settles as final, the light falls on it, and it gets its check mark. Checked facts are chips under it.
3. **Scope and boundaries.** No chat bubbles, no avatars, no streaks. Not a medical device. Pipeline, events, store, api, live and link code untouched; only markup classes, CSS, icons and presentational components change.
4. **States.** connecting, waiting, listening, other speaking (partial), draft, final, checking, verified, unsure, mismatch + read-back, say again, off the record, reconnecting, disconnected, ended → summary, burned; headphones vs speaker; push-to-talk idle/held/sending/blocked.
5. **Constraints.** Self-hosted fonts, no third-party requests. WCAG AA in both themes; colour never the only state signal; prefers-reduced-motion; Arabic RTL content.

## Direction contract

THESIS: RAUM is the Berlin Altbau room the practice really sits in, in daylight: the newest sentence stands in the window's light, drafts wait in the shade. It refuses chat bubbles and the dark glowing AI dashboard.

OWN-WORLD: Kalkweiß limewash wall (#f4f4f1) with cool daylight shade; walnut ink (#211b17); each room's oil-paint dado owns the lower shell (Sprechzimmer reseda #6f845a, Kanzlei oxblood #7a2f28, Verhandlungsraum cobalt #2d4f96) under one 4 px walnut Begleitstrich. Sofia Sans for words, Sofia Sans Extra Condensed uppercase for room plates, wordmark and big figures. Sheets with 6 px corners, pills for status. Pens: green checked, amber unsure, red mismatch. Raises: bilateral centre line (Labanotation); hairline module density in glass box and Admin (High-density Web); one depth rule, wall / sheet / lifted sheet (Acetate Manual); one visible structural line on a fixed module (Crouwel); turns never move, drafts change in place (Split-flap).

STORY: The listener sees whose turn it is by the room-coloured speaker plate, watches the draft form in the shade, sees the daylight fall across it as it becomes final and tick, reads facts as chips, answers a read-back with one tap.

FIRST VIEWPORT: Phone 390×844. Top on the wall: room plate in condensed caps ("CONSULTATION ROOM"), German name and id beneath, Live pill, menu; a hairline language strip (You speak · German | Patient speaks · Arabic | You hear on · Speaker). Centre: the hero sheet in the light patch, speaker plate + check state, sentence at 30–32 px, source line, fact chips. Bottom: the dado in the room colour under the Begleitstrich: 92 px limewash mic orb with level halos, labelled side actions.

FORM: Altbau Daylight, candidate 7 of 7 on the ordered list (the literal room reading), assigned by the roll. seed:f5337174a914b648. Signature interaction: Lichtfall, draft → final in 420 ms (a diagonal band of daylight sweeps across the hero sheet and rests as warm light, ink darkens, dotted draft underline retracts, tick draws).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Finish review (3 Nov 2026)

impeccable-finish-reviewer on the shipped build (5aed352 + 80750af): **ship after the listed fixes**. Fidelity matched the contract on wall/dado/Begleitstrich, header plate and strip, Lichtfall hero and chips, lifted read-back, Join blocks, Home doors, type and material. Material fixes, all made and confirmed in one capture round (phones 360/390 light + dark, Table 1440):

1. Talk: the hero's top stays in view under a read-back or the reader's own reply; one banner at a time on phones.
2. Join: the AI disclosure in words on the dado above the button (the consent sheet sat under the dado at 360/390).
3. Dark theme: the sun crossing the wall at 0.10/0.04 (it dropped ink-3 to 2:1).
4. The other person's speaker plate in the room's paint (`--them` resolved on `:root` to the default paint).
5. Summary: a phone in neither conversation language never sees one language twice; the take-home QR carries `?lang=`.
6. Table: fact chips in the cell of their language, never across the centre line.
7. Table: 3 m floor of 22 px for check marks, fact values, latency figures and the privacy-meter label.
8. Summary sheet head: the walnut line above the paint (Paint Below The Line).

Also in this pass: summary headings in the reader's language; on phones the link pill keeps only its mark while the banner names a dropped link (the room id was cut at 360 px); the dado's three labels on one line. Detector: two advisory overshoot easings in `spectacle.css` (chip pop, read-back hand-in), kept as the redesign's deliberate spectacle motion.

## Talk refinement and finish review (3 Nov 2026, design/room)

The inside of the room was refined within the same world; no tokens or other screens changed. The changes: a ruled fact strip, a "Facts noted" ledger, the read-back as a slip at the hero's foot with the whole sheet lifted, Yes/No at 52 px with glyphs, the proof as a ruled section, a language code on the source line, a three-bar voice meter in the plate, the bell-push orb, the strip's hairline as the scroll edge, and a compact off-the-record pill on phones. The impeccable-finish-reviewer verdict was **ship after fixes**. Fixed: the language tag, the hero time stamp and other labels on the hero's light patch now use ink-2 (ink-3 measured 4.27:1 in dark); the Hands-free label is white (paint-ink-2 measured 4.1:1). Its read-back-under-the-dado finding came from the capture harness (390 → 360 resize without a fresh settle). A fresh 360×780 phone shows the hero top and Yes/No in view, so Talk.tsx is unchanged.

## Join: across the desk (3 Nov 2026)

Surface redesign of Join `/j/:room` inside the fixed Altbau Daylight world (user: "redesign the Join screen, make it more premium"; files Join.tsx + join.css; no tokens or other screens). Unattended: the user said to proceed, so no decision round was shown.

THESIS: Join is the doorway of the room: the door plate, "You join as Patient", then two seats across one desk split by one centre line, the box sitting on that line as its one key. Your seat stands in the window's light with your language in its own script; one tap on the dado sits you down. Refuses the stacked settings form it replaced.

FORM: surface structure 3 of 7 grounded candidates ("Across the desk"), dealt by the roll. seed:0f1cc6721a53d487. Raises: centre-rail reference page (declined): the rail is the desk's one moving part, the box a square key cut into it; terminal wayfinding (declined): a phone shows only the decision ahead, so language and hearing collapse to their value behind one Change. The voice-copy choice stays on screen (consent).

SIGNATURE: Platz nehmen. The desk assembles once: the rail draws down, the box key sets on it, daylight falls across your seat. A changed language or hearing re-keys in place and the light falls again; joining runs the rail dashed toward the box.

Finish review (impeccable-finish-reviewer): first pass recapture (one viewport shot labelled full page), then **fix**, eight material fixes: same-language note and voice choice moved into the desk under the key; dark `seat__en` / hearing icon to ink-2 (4.36:1 → AA); the chooser opens under the key so the key never leaves the rail; the repeating button glint removed; this FORM line; room-not-found without seats, role or a repeated id; the seat control reads Close while open; the dado button on `.btn--wall`. Also the empty seat waits in the shade, and the dado stands on the floor when the wall is short.

