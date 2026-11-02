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
