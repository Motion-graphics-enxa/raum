---
name: RAUM
description: The conversation drawn as a floor plan in Berliner Blau. Translates. Checks. Forgets.
colors:
  paper: "#f6f9fb"
  paper-2: "#eaf0f5"
  paper-3: "#dde6ee"
  ink: "#0a1d30"
  ink-2: "#33495f"
  ink-3: "#566c82"
  rule: "#c3d0dc"
  rule-strong: "#8fa3b6"
  brand: "#0d3a61"
  brand-2: "#164f82"
  brand-ink: "#eef6fc"
  brand-ink-2: "#b9d3e8"
  brand-rule: "#2f5f8a"
  link: "#164f82"
  focus: "#1b6fd1"
  ok: "#157347"
  unsure: "#925400"
  unsure-wash: "#f7ecd9"
  bad: "#c01d2c"
  bad-wash: "#fbe5e6"
  pending: "#6a7f94"
  room-sprechzimmer: "#0a6f80"
  room-kanzlei: "#5945a4"
  room-verhandlungsraum: "#a03670"
  band-sprechzimmer: "#5fd0de"
  band-kanzlei: "#b6a6ff"
  band-verhandlungsraum: "#ff9fd0"
  paper-dark: "#0b2a45"
  paper-2-dark: "#0f3352"
  paper-3-dark: "#143c5f"
  ink-dark: "#eaf4fb"
  ink-2-dark: "#b4cde0"
  ink-3-dark: "#8eadc5"
  rule-dark: "#28557b"
  rule-strong-dark: "#4b7aa2"
  brand-dark: "#071d33"
  brand-ink-2-dark: "#9fc2dc"
  brand-rule-dark: "#24507a"
  link-dark: "#9fd3ff"
  ok-dark: "#63d69e"
  unsure-dark: "#f3b85b"
  unsure-wash-dark: "#3a3a32"
  bad-dark: "#ff8585"
  bad-wash-dark: "#43303f"
typography:
  display:
    fontFamily: "Atkinson Hyperlegible Next Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "clamp(1.75rem, 1.1rem + 2.6vw, 2.125rem)"
    fontWeight: 640
    lineHeight: 1.2
    letterSpacing: "-0.012em"
  headline:
    fontFamily: "Atkinson Hyperlegible Next Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "1.625rem"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Atkinson Hyperlegible Next Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "1.3125rem"
    fontWeight: 560
    lineHeight: 1.3
  body:
    fontFamily: "Atkinson Hyperlegible Next Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "\"tnum\""
  source:
    fontFamily: "Atkinson Hyperlegible Next Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.45
  figure:
    fontFamily: "Atkinson Hyperlegible Mono Variable, Noto Sans Arabic Variable, ui-monospace, monospace"
    fontSize: "1rem"
    fontWeight: 640
    lineHeight: 1.3
  label:
    fontFamily: "Atkinson Hyperlegible Mono Variable, Noto Sans Arabic Variable, ui-monospace, monospace"
    fontSize: "0.75rem"
    fontWeight: 560
    letterSpacing: "0.07em"
  wordmark:
    fontFamily: "Atkinson Hyperlegible Next Variable, system-ui, sans-serif"
    fontSize: "clamp(2.75rem, 2rem + 4vw, 4.5rem)"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "0.08em"
rounded:
  drawing: "2px"
  stamp: "3px"
  round: "50%"
spacing:
  s-1: "4px"
  s-2: "8px"
  s-3: "12px"
  s-4: "16px"
  s-5: "24px"
  s-6: "32px"
  s-7: "48px"
components:
  button-primary:
    backgroundColor: "{colors.brand}"
    textColor: "{colors.brand-ink}"
    rounded: "{rounded.drawing}"
    padding: "0 24px"
    height: "48px"
  button-primary-hover:
    backgroundColor: "{colors.brand-2}"
  button-primary-dark:
    backgroundColor: "{colors.ink-dark}"
    textColor: "{colors.paper-dark}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.drawing}"
    padding: "0 24px"
    height: "48px"
  button-ghost-hover:
    backgroundColor: "{colors.paper-2}"
  button-ok:
    backgroundColor: "{colors.ok}"
    textColor: "#ffffff"
    rounded: "{rounded.drawing}"
    height: "48px"
  button-bad:
    backgroundColor: "transparent"
    textColor: "{colors.bad}"
    rounded: "{rounded.drawing}"
    height: "48px"
  button-bad-hover:
    backgroundColor: "{colors.bad-wash}"
  talk-control:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.brand}"
    rounded: "{rounded.round}"
    size: "96px"
  talk-control-on:
    backgroundColor: "{colors.brand}"
    textColor: "{colors.brand-ink}"
  title-block:
    backgroundColor: "{colors.brand}"
    textColor: "{colors.brand-ink}"
    padding: "8px 16px 0"
    height: "72px"
  verify-mark:
    backgroundColor: "transparent"
    typography: "{typography.label}"
    rounded: "{rounded.drawing}"
    padding: "2px 10px 2px 6px"
    height: "28px"
  proof-panel:
    backgroundColor: "{colors.paper-2}"
    rounded: "{rounded.drawing}"
    padding: "16px"
  confirm-sheet:
    backgroundColor: "{colors.paper}"
    padding: "24px"
    width: "640px"
  burn-stamp:
    textColor: "{colors.bad}"
    rounded: "{rounded.stamp}"
    padding: "8px 16px 12px"
---

# Design System: RAUM

## Overview

**Creative North Star: "The Architect's Floor Plan in Berliner Blau"**

RAUM draws a conversation the way an architect draws a room. Every surface is a drawing sheet: line work in Prussian blue on drafting-film white (the Lichtpause, light theme), or pale cyan-white line work on a Prussian ground (the Blaupause, dark theme). Metadata sits in title-block cells (the Schriftfeld). Every checked fact is dimensioned like a wall, with a value lettered above a line that ends in 45° architectural ticks. A fact that does not close gets a revision cloud, the way a drawing marks a change. Each room is a material hatch with one layer colour. At the end the session is erased stroke by stroke and the title block is stamped "Schlüssel vernichtet".

The system is dense and technical where it reports, and calm where it speaks. On a phone, the other person's newest sentence is the largest thing on screen and everything else steps down one fixed recency ramp. Verification state is drawn as DIN 1356 line types before it is coloured, so every state reads in greyscale. Colour is held back: Berliner Blau for the shell, three semantic pens for check state, three layer colours for rooms, and nothing decorative. It rejects the category default of chat bubbles on clinical white with a teal accent and a glowing AI badge.

**What transfers to every future surface** (landing page, slides, video, print): both prints and their palettes; the Prussian title band as the header of any frame; Atkinson Hyperlegible Next for words and Mono for every figure and label; the dimension line for any number that is claimed (latency, bytes, doses), drawn to scale against the 1.0 s dash-dot budget wherever time is shown; the revision cloud for anything caught; the room hatch and layer colour as the room's identity; the drawing border (a 2 px ink frame with a 1 px inner margin line 6 px in); and the burn (erase, then stamp) as the closing beat. Motion on video follows the same grammar: lines draw from tick to tick and nothing bounces.

**Key Characteristics:**
- Two prints of one drawing: Lichtpause light, Blaupause dark, same structure.
- State is line form first (solid, dashed, dotted, revision cloud), colour second.
- One recency ramp carries hierarchy; nothing else decorates rank.
- Every figure in mono, dimensioned, with its unit.
- Corners are drawn corners (2 px); only round objects are round.
- Logical (inline-start/end) geometry throughout, so the drawing mirrors in RTL.

## Colors

A Prussian-blue drawing with three semantic pens and three room layers; every value has a light (Lichtpause) and a dark (Blaupause) print, switched by `prefers-color-scheme` or `data-theme`.

### Primary
- **Berliner Blau** (brand): the title-block band, the primary button, the talk control when live. In the Blaupause it deepens to Prussian Night (brand-dark), so the band stays darker than the sheet.
- **Pressed Blue** (brand-2): hover on primary buttons and the talk control's contour lines.
- **Band Lettering** (brand-ink, brand-ink-2): text and secondary text on the Prussian band; Band Rule (brand-rule) divides title-block cells.
- **Drawing Link** (link): the only interactive ink on the sheet: links, the typing caret, text selection, the occupied seat on the floor plan, the sent "explain simply" box. Turns Cyan Trace (link-dark) on the blueprint.

### Secondary (semantic pens)
- **Verified Green** (ok / ok-dark): solid dimension lines, the verified mark, the confirm "yes" button, the zero on the privacy meter, the budget mark when first words land under 1.0 s.
- **Unsure Ochre** (unsure / unsure-dark): dotted dimension lines, the unsure and "say again" marks, the confirm sheet's top edge, the budget mark when over 1.0 s.
- **Mismatch Red** (bad / bad-dark): revision clouds, struck-through wrong values, the mismatch mark, the "no" button, the burn stamp.
- **Pending Slate** (pending): dashed dimension lines while the check runs.
- **Washes** (unsure-wash, bad-wash): only system banners (connection warning, failure) and the "no" button's hover. Never a fact row.

### Tertiary (room layers)
- **Gespräch Ink** (room-gespraech = the drawing ink; band-gespraech = band ink): the everyday room runs no second check, so it takes no colour of its own; its identity is the hatch alone.
- **Sprechzimmer Teal** (room-sprechzimmer), **Kanzlei Violet** (room-kanzlei), **Verhandlungsraum Magenta** (room-verhandlungsraum): each room's hatch and its room-specific line work (the offer chain and offer bars in Verhandlungsraum). On the Prussian band, which is dark in both themes, the lighter **band** layer values (band-sprechzimmer, band-kanzlei, band-verhandlungsraum) are used; on the blueprint sheet the room layers take those same light values.

### Neutral
- **Drafting Film** (paper), **Film Shade** (paper-2), **Film Fold** (paper-3): the sheet, recessed panels (proof, glass box, consent note, active dock button), and the hatch stripe of the off-the-record banner.
- **Blue-Black Ink** (ink), **Ink 2** (ink-2), **Ink 3** (ink-3): text at three strengths. Ink 3 carries the source sentence, labels and older metadata.
- **Hairline** (rule) and **Construction Line** (rule-strong): turn separators, inner sheet margins, ghost-button borders, the left margin line of the talk scroll.
- **Focus Blue** (focus): the 2 px focus outline, deliberately apart from Berliner Blau.

### Named Rules
**The Colour Quarantine Rule.** Check-state colour lives only on the line, cloud, mark or stamp that carries the fact. A verified, unsure or mismatched fact never tints its row or its card.

**The Greyscale Test Rule.** Every state must read with colour removed: solid = verified, dashed = checking, dotted = unsure, revision cloud = mismatch, greyed-out drawing = off the record.

**The Separate Pens Rule.** Semantic pens never stand in for the brand, and Berliner Blau never signals a check state.

## Typography

**Display Font:** Atkinson Hyperlegible Next Variable (with Noto Sans Arabic Variable, then system-ui)
**Body Font:** Atkinson Hyperlegible Next Variable (same stack)
**Label/Mono Font:** Atkinson Hyperlegible Mono Variable (with Noto Sans Arabic Variable, then ui-monospace)

All three are self-hosted through @fontsource; the app makes no font request to a third party.

**Character:** A hyperlegible sans for the words people say, and its mono sibling for every number, unit, id and label, the lettering of a technical drawing. Digits are unambiguous by design (500 never reads as 5OO), and tabular figures are on by default.

### Hierarchy
- **Display** (640, 28 to 34 px fluid, 1.2): the hero, the other person's newest sentence, typing in. On the Table screen it scales to clamp(1.5rem, 2.75rem).
- **Headline** (650, 26 px, 1.3): the one question on the confirm sheet; the consent title (720, 24 px) and empty-state title (700, 24 px) sit at this level.
- **Title** (560, 21 px, 1.3): the previous turn (ramp step 1), the "explain simply" text, the "please say that again" line.
- **Body** (400 to 560, 16 to 18 px, 1.35 to 1.5): older turns at ramp steps 2 and 3 (18 px, 16 px); your own turns at 18 px in ink 2. Running text max 60ch.
- **Source** (400, 17 px, 1.45, ink 3): the original sentence under the translation, in the speaker's script.
- **Figure** (mono 640, 16 px): dimension values and cloud pairs; timeline figures in mono 650 at 13 px.
- **Label** (mono 560 to 700, 12 px, 0.07em, uppercase): speaker names, title-block cell headings (10 px), fact kinds (11 px), the verify mark, section heads on the summary sheet (13 px, 700).
- **Wordmark** (800, 44 to 72 px, 0.08em): "RAUM" on Home and Join only.

### Named Rules
**The Recency Ramp Rule.** Hierarchy in the conversation is carried by one fixed ramp (34, 21, 18, 16 px). The newest sentence of the other person is largest; each older turn steps down. Weight, colour and boxes never add rank.

**The Mono Figures Rule.** Every figure, unit, id, timing and label is set in Atkinson Hyperlegible Mono. Words are never set in mono, except the uppercase lettering of labels.

**The Arabic Lettering Rule.** Arabic, Persian and Urdu text gets zero letter-spacing (forced) and a 1.6 line height in turns.

## Layout

The phone is a single column on a drawing sheet: the Prussian title block at the top (72 px with its cells), the scrolling conversation in the middle, and a dock at the bottom with the talk control centred between two side actions on a 1fr / auto / 1fr grid. The scroll area carries a 1 px margin line 12 px from the start edge (it moves to the other side in RTL), and earlier turns fade into the sheet under a 36 px top mask. Turns are separated by hairlines with 16 px padding; there are no cards.

Spacing is a 4 px base scale (4, 8, 12, 16, 24, 32, 48). Content widths: Join 560 px, conversation 720 px, Summary sheet 760 px, Home 880 px, bottom sheets 640 px. The Table screen fills the viewport (no page scroll), sets its base size with clamp(16px, 0.55rem + 0.75vw, 22px) so it reads from 3 m, and splits into conversation (1.7fr) and glass box (minmax(320px, 1fr)); below 860 px it stacks. Admin collapses its grids to two columns below 760 px.

**RTL.** The document direction follows the reader's language. Every edge uses logical properties (inline-start, inline-end), dimension lines draw from the start tick, the burn erases from the start edge, and mixed-direction values (dates, doses, ids) are isolated with `unicode-bidi: isolate` or `<bdi>`.

## Elevation & Depth

The drawing is flat: depth comes from line weight and from the paper tones (paper, paper-2, paper-3), not shadow. A soft sheet shadow lifts only physical paper objects: the summary sheet, the talk control (at rest) and sheets that slide up from the bottom. Scrims behind bottom sheets are Prussian, not black.

### Shadow Vocabulary
- **Sheet** (`box-shadow: 0 1px 2px rgb(10 29 48 / 0.06), 0 8px 24px rgb(10 29 48 / 0.08)`; darker in the Blaupause): the summary sheet and the resting talk control.
- **Sheet rising** (`box-shadow: 0 -12px 32px rgb(6 20 34 / 0.18)`): the confirm sheet.
- **Stamp impression** (`box-shadow: inset 0 0 0 1.5px` bad at 35%): the double rim of the burn stamp, inside the stamp's own edge.

### Named Rules
**The Paper Objects Rule.** A shadow means "a sheet of paper lies on top". Line work, marks and panels never cast one.

## Shapes

Drawings have corners: every rectangle uses a 2 px radius, the burn stamp 3 px. Only objects that are round in the world are round: the talk control, the live status dot, the verify mark's ring. Borders do the structural work. A 1 px hairline separates; a 1.5 px line draws a mark or plate; a 2 px ink line frames a sheet or a primary button. The DIN 1356 line types (solid, dashed, dotted, dash-dot) recur wherever state or kind must be read: dimension lines, verify marks, connection plates (direct solid, tunnel dashed, relayed dotted), talk-control contours, the async check segment, the 1.0 s budget line. Notes get corner ticks (2 px L-brackets at two opposite corners) instead of a full box.

## Components

### Buttons
Drawn, not pillowed: flat fills with a 2 px border in the fill colour.
- **Shape:** drawing corners (2 px); 48 px tall (60 px for the large variant), 24 px horizontal padding, 650 weight at 17 px.
- **Primary:** Berliner Blau with band lettering. In the Blaupause it inverts to an ink button with paper text.
- **Hover / Focus:** hover moves to Pressed Blue in 140 ms ease-out; press nudges 1 px down; focus is the global 2 px Focus Blue outline at 2 px offset.
- **Ghost:** transparent with a Construction Line border; hover fills Film Shade.
- **Answer pair (confirm sheet):** "yes" in Verified Green with white (dark: deep green) text; "no" as a red outline that fills with the red wash on hover. Equal width, side by side.

### Title Block (Schriftfeld)
The fixed name plate of every phone screen and the Table screen. A Prussian band: room hatch swatch (22 px) and German room name (700, 18 px) with the translated gloss, a mono status ("LIVE" with a filled green dot, otherwise an outlined dot), and the menu. Below it, a row of cells divided by Band Rule: room id, language pair "DE ⇄ AR", connection path with round-trip ms. Off the record, the band turns slate grey. The Summary sheet ends in a light-print title block of four ink-ruled cells (room, language, drawn by, checked by).

### Dimension (checked fact)
The signature component. Value in mono above, a 12 px line with an extension line and a 45° tick (1.75 px) at each end, the fact kind in 11 px mono below. Line type is the state: solid green verified, dashed slate checking (ticks at half opacity), dotted 2 px ochre unsure. **Motion:** the line draws from the start tick to the end tick (scaleX, 220 ms, ease-out, staggered 60 ms per fact), the end ticks fade in 40 ms before the line completes, and the value settles 3 px upward 120 ms after the line starts.

### Revision Cloud (mismatch)
A scalloped SVG outline (arc radius about 5.5 px) sized to its content, drawn in Mismatch Red at 1.6 px. It holds the pair: what was said in ink, "≠", and what was translated in red, struck through. The cloud draws itself around the pair in 520 ms. A mismatch is the only interruption: it also opens the confirm sheet.

### Verify Mark
The check state of one sentence, as a 28 px mono uppercase tag with the turn's measured time beside it. Translating: no border, a spinning half ring. Checking: dashed border. Verified: solid 1.5 px green with a check glyph. Unsure and "say again": dotted 2 px ochre. Mismatch: solid 2 px red with the revision mark. When tappable it opens the proof panel (dimensioned pairs, back-translation, similarity score and the timeline), on Film Shade with a hairline border.

### Talk Control
One dominant control: a 96 px circle on a 2 px Berliner Blau ring with a 36 px microphone. Live, it fills with Berliner Blau; in the Blaupause it is an ink ring that fills with ink. The voice level is drawn as three concentric contour lines (solid, dashed, dotted) that grow with the level (radius transitions 90 ms linear). Disabled, it greys to Construction Line on Film Shade and drops its shadow. A mono uppercase label sits beneath. Off the record and the room's action sit on either side as 64 px mono-lettered dock buttons.

### Confirm Sheet
Slides up 24 px in 280 ms over a Prussian scrim, with a 3 px top edge in ochre (unsure) or red (mismatch). It shows the cloud or dimension at 18 px, one question at headline size, and the two answers.

### Timeline (the turn drawn to scale)
Each turn's pipeline as a dimension chain on a true time axis: segments for endpoint, transcript, first words and full sentence in solid ink 2, the async check dashed, each starting with a 45° tick. Beneath it, the 1.0 s target is drawn in the dash-dot line type with an end tick, a 3 px budget mark at the first-words time (green under budget, ochre over) and a thinner voice mark. A legend repeats each segment's line type. The Table screen uses a larger variant (2.5 px lines, 20 px ticks).

### Room Hatch
Each room is a 12 px material hatch inside a square frame: Sprechzimmer single diagonal, Kanzlei cross-hatch, Verhandlungsraum dots, Gespräch plain horizontal courses (the open room), in the room's layer colour. Rooms list in `order` (Sprechzimmer first). Verhandlungsraum also draws offers as a dimension chain in magenta and an offer timeline of hatched bars (the other side's bars dashed).

### Summary Sheet and the Burn
The summary is a drawing sheet: a 2 px ink frame, a 1 px inner margin line 6 px in, mono section heads underlined in ink, dashed hairlines between items, uncertain items underlined in dotted ochre, and the title block at the foot. **The burn:** each section is erased toward the inline end (clip-path, 520 ms, staggered 90 ms) to 20% opacity, then the drawing is replaced by an empty hatched frame and the stamp lands on the title block's top rule: "SCHLÜSSEL VERNICHTET" in mono 800, the time and the audit hash, in a 3 px red border rotated −3°, scaling in from 1.18 over 420 ms. On the Table screen's glass box the same stamp sits in line.

### Glass Box (Table screen)
Recessed in Film Shade, divided by Construction Lines: the privacy meter as a large green figure (48 to 96 px, 800), connection plates bordered by path line type, model list, timeline and stamp.

### Motion
One easing for everything: ease-out cubic-bezier(0.16, 1, 0.3, 1). Draw time 220 ms. Turns enter 6 px up in 240 ms; overlays fade in 160 to 180 ms. Under `prefers-reduced-motion`, every animation and transition is cut to 1 ms and runs once, so lines, clouds and stamps appear already drawn.

## Do's and Don'ts

### Do:
- **Do** dimension every checked figure: value above, line with 45° ticks, kind below, in mono.
- **Do** carry state in line type first (solid, dashed, dotted, revision cloud) and in the semantic pen second, with the state named in text on the verify mark.
- **Do** keep the newest sentence of the other person the largest text on the phone, on the fixed ramp (34, 21, 18, 16 px).
- **Do** use the band layer colours on the Prussian title band and the room layer colours on the light sheet.
- **Do** use logical properties and isolate mixed-direction values, so the whole drawing mirrors in RTL.
- **Do** keep shadows for paper objects only (summary sheet, talk control, rising sheets).
- **Do** letter labels in mono uppercase at 0.07em, and only when they name the value next to them.

### Don't:
- **Don't** draw chat bubbles, avatars, or a glowing AI badge; turns are lines of text on the sheet, separated by hairlines.
- **Don't** tint a fact's row, card or background with its check colour; washes belong to system banners only.
- **Don't** use colour as the only signal of a state.
- **Don't** use Berliner Blau to mean verified, or a semantic pen as a brand accent.
- **Don't** round rectangles beyond the 2 px drawing corner; only round objects are round.
- **Don't** letter-space Arabic, Persian or Urdu.
- **Don't** load fonts or assets from a third party; the privacy meter must stay at zero with the cable pulled.
