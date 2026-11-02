---
version: 1
slug: "app-src-screens-talk-tsx"
primary_target: "app/src/screens/Talk.tsx"
related_targets: ["app/src/screens/Join.tsx","app/src/screens/Summary.tsx","app/src/screens/Table.tsx","app/src/screens/Admin.tsx"]
---

## Scope

Talk screen `/r/:room` (Operate, phone, portrait 360–430 px) and, sharing one world: Join `/j/:room`, Summary `/r/:room/summary`, Table screen `/t/:room` (laptop/monitor, read from 3 m), Admin `/admin`, listen-only `/l/:room`, Home `/`. Redesign of 2 Nov 2026: the user pinned "glass style, premium, clearer, all UI in English". UI chrome is English on every device; spoken and translated content stays in each participant's language.

## Shape brief (Talk)

1. **Job and audience.** Two people across a desk, each on their own phone: the professional and the other party (any language, possibly older, anxious, RTL). Mode: Operate. Each phone answers at a glance: who is speaking, what did they just say in my language, is it draft or final, and can I trust it?
2. **Outcome and proof.** The other person's newest sentence in my language is the hero; while it streams it is a draft, then it settles as final and gets its check mark. Checked facts (dose, frequency, allergy, date, amount) are chips under it.
3. **Scope and boundaries.** No chat bubbles, no avatars, no streaks. Not a medical device. Pipeline, events and store untouched.
4. **States.** connecting, waiting, listening (my mic), other speaking (partial), draft translation, final, checking, verified, unsure, mismatch + read-back, say again, off the record, reconnecting, disconnected, ended → summary, burned; headphones vs phone speaker.
5. **Constraints.** Self-hosted fonts, no third-party requests. WCAG AA in both themes including text over glass; colour never the only state signal; prefers-reduced-motion; solid fallback without backdrop-filter.

## Direction contract

THESIS: RAUM is the frosted glass door of a Berlin practice (Milchglas): you see that a conversation is happening, never what is said. Draft words sit behind the frost; when a sentence is final the glass clears. It refuses chat bubbles on clinical white with a glowing AI badge.

OWN-WORLD: Berliner Blau stays the brand. A deep Prussian-to-teal light field (dark) or a pale mist field (light) behind frosted panes: translucent white or navy fill, 24 px blur, 1 px light rim, inner top highlight, soft offset shadow. 20 px pane corners, 14 px controls, pill chips. Atkinson Hyperlegible Next for words and sentence-case labels; Atkinson Mono only for figures. Semantic pens green / ochre / red stay separate from the brand; room accent tints the field.

STORY: The listener sees whose turn it is by a coloured speaker plate, watches the draft form behind the frost, sees it clear and tick, reads the facts as chips, and answers a read-back with one tap.

FIRST VIEWPORT: Phone 390×844. Floating glass header (room name + English gloss, Live pill, menu) with a language strip "You · German → hear · German / Patient · Arabic" and the audio route (headphones or speaker). Centre: the hero pane with speaker plate, the sentence at 30–34 px, source line under it, fact chips. Bottom: a glass dock, 88 px mic orb with a level halo and a state label, side actions as labelled glass buttons.

FORM: Milchglas (frosted door glass), brief-pinned material translated onto the incumbent RAUM topology (title block, recency ramp, dock, read-back). seed: none (unavailable). Signature interaction: the frost clears, draft → final in 280 ms (pane frost lifts, ink darkens, dotted draft underline retracts).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
