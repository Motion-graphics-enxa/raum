---
name: RAUM
description: Altbau Daylight. The Berlin Altbau room the practice really sits in; the newest sentence stands in the window's light. Translates. Checks. Forgets.
colors:
  wall: "#f4f4f1"
  wall-2: "#ebebe6"
  wall-3: "#e1e2dc"
  shade: "#e4e7e6"
  sheet: "#fcfcfa"
  sheet-2: "#f7f7f3"
  sun: "rgb(255 224 168)"
  mullion: "rgb(120 92 52 / 0.07)"
  ink: "#211b17"
  ink-2: "#4d443d"
  ink-3: "#6b6158"
  rule: "rgb(33 27 23 / 0.12)"
  rule-strong: "rgb(33 27 23 / 0.26)"
  strich: "#211b17"
  link: "#2d4f96"
  focus: "#2d4f96"
  brand: "#211b17"
  brand-2: "#3a312a"
  brand-ink: "#f8f7f3"
  paint-sprechzimmer: "#5d7350"
  paint-kanzlei: "#7a2f28"
  paint-verhandlungsraum: "#2d4f96"
  paint-gespraech: "#4f6475"
  paint-off: "#6f675f"
  paint-ink: "#ffffff"
  paint-ink-2: "rgb(255 255 255 / 0.82)"
  paint-well: "rgb(255 255 255 / 0.14)"
  paint-rim: "rgb(255 255 255 / 0.42)"
  ok: "#1d6b3a"
  unsure: "#8f5300"
  bad: "#b4231b"
  pending: "#6b6158"
  live: "#23934f"
  scrim: "rgb(33 27 23 / 0.42)"
  wall-dark: "#1c1916"
  wall-2-dark: "#24201c"
  shade-dark: "#171512"
  sheet-dark: "#272220"
  ink-dark: "#f3efe8"
  ink-2-dark: "#d4cabe"
  ink-3-dark: "#ab9f92"
  strich-dark: "#0c0a09"
  link-dark: "#a9c1f2"
  ok-dark: "#7fd39b"
  unsure-dark: "#f0b860"
  bad-dark: "#ff8a7a"
typography:
  plate:
    fontFamily: "Sofia Sans Extra Condensed Variable, Sofia Sans Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "clamp(1.625rem, 1.2rem + 1.6vw, 2rem)"
    fontWeight: 780
    lineHeight: 0.9
    letterSpacing: "0.012em"
  wordmark:
    fontFamily: "Sofia Sans Extra Condensed Variable, Sofia Sans Variable, system-ui, sans-serif"
    fontWeight: 860
    lineHeight: 0.82
    letterSpacing: "0.02em"
  display:
    fontFamily: "Sofia Sans Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "clamp(1.75rem, 1.15rem + 2.4vw, 2.125rem)"
    fontWeight: 720
    lineHeight: 1.18
    letterSpacing: "-0.014em"
  title:
    fontFamily: "Sofia Sans Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "1.3125rem"
    fontWeight: 620
    lineHeight: 1.3
  body:
    fontFamily: "Sofia Sans Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 450
    lineHeight: 1.5
    fontFeature: "\"tnum\", \"case\""
  turn:
    fontFamily: "Sofia Sans Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 560
    lineHeight: 1.38
  label:
    fontFamily: "Sofia Sans Variable, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 650
    letterSpacing: "0.01em"
  figure:
    fontFamily: "Sofia Sans Semi Condensed Variable, Sofia Sans Variable, system-ui, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 760
    fontFeature: "\"tnum\""
rounded:
  sm: "4px"
  md: "6px"
  lg: "6px"
  pill: "999px"
  round: "50%"
spacing:
  s-1: "4px"
  s-2: "8px"
  s-3: "12px"
  s-4: "16px"
  s-5: "24px"
  s-6: "32px"
  s-7: "48px"
  s-8: "64px"
  strich: "4px"
components:
  sheet:
    backgroundColor: "{colors.sheet}"
    rounded: "{rounded.lg}"
    padding: "16px"
  hero-sheet-draft:
    backgroundColor: "{colors.shade}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.lg}"
  hero-sheet-final:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
  dado:
    backgroundColor: "{colors.paint-gespraech}"
    textColor: "{colors.paint-ink}"
  button-primary:
    backgroundColor: "{colors.brand}"
    textColor: "{colors.brand-ink}"
    rounded: "{rounded.md}"
    height: "52px"
    padding: "0 24px"
  button-primary-hover:
    backgroundColor: "{colors.brand-2}"
  button-ghost:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "52px"
  button-ghost-hover:
    backgroundColor: "{colors.wall-2}"
  button-wall:
    backgroundColor: "{colors.wall}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "52px"
  talk-orb:
    backgroundColor: "{colors.wall}"
    textColor: "{colors.ink}"
    rounded: "{rounded.round}"
    size: "84px"
  talk-orb-live:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paint-ink}"
    rounded: "{rounded.round}"
    size: "84px"
  speaker-plate-them:
    backgroundColor: "{colors.paint-gespraech}"
    textColor: "{colors.paint-ink}"
    rounded: "{rounded.pill}"
    height: "26px"
  speaker-plate-you:
    backgroundColor: "{colors.wall-3}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.pill}"
    height: "26px"
  fact-chip:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "7px 13px 8px"
  verify-mark:
    backgroundColor: "{colors.sheet}"
    rounded: "{rounded.pill}"
    height: "30px"
  status-pill:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.pill}"
    height: "32px"
  banner:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "11px 16px 11px 14px"
  segmented-option:
    backgroundColor: "{colors.wall-2}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "72px"
  segmented-option-selected:
    backgroundColor: "{colors.sheet}"
---

# Design System: RAUM

## Overview

**Creative North Star: "Altbau Daylight"**

RAUM is the Berlin Altbau room a practice or law office really sits in, seen in morning daylight. A limewash wall above; below it, the room's oil-paint dado; between them one dark Begleitstrich, 4 px of walnut that is the system's one visible structural line. The interface is the room; the words are written on sheets pinned to its wall. The other person's newest sentence stands on a sheet in the window's light. Drafts wait in the cool shade, and when a sentence becomes final the daylight falls across it: **Lichtfall**, the signature.

Calm, plain, trustworthy, and literal: every material is something actually in the room (limewash, oil paint, walnut, paper, daylight, the shadow of the window cross). Light theme is the room in the morning; dark theme is the same room in the evening, warm walnut, never a techy navy-black. No chat bubbles, no avatars, no glowing AI dashboard, no glass or blur.

**All UI chrome is English** on every device (`UI_LANG = "en"` in `app/src/lib/i18n.ts`; `t()` and `pick()` read it whatever the phone's language). Content stays in each person's language: what they said, its translation, the read-back question. The language picker names each language in its own script with the English name beside it ("العربية · Arabic"). The chrome runs left to right; every content element carries its own `lang` and `dir`, so Arabic content still runs right to left. Arabic, Persian and Urdu get zero letter-spacing, no uppercase, and a 1.6 line height.

**Key Characteristics:**
- Wall above, paint below, one 4 px walnut Begleitstrich between them, on every screen that has an action surface (Talk dock, Join and menu sheets, Table floor, Home doors, the room mark).
- Each room owns its paint: Sprechzimmer reseda, Kanzlei oxblood, Verhandlungsraum cobalt, Gespräch dove blue. The paint lives in the dado, the other person's speaker plate, the caret and the selected radio square; nowhere else.
- Three depths only: the wall, a sheet on the wall, a sheet lifted for an interruption.
- Hierarchy is one recency ramp (34, 21, 18, 17 px). Turns never move once placed; only their words change.
- State is drawn twice: line type first (solid, dashed, dotted), colour second, and always named in words.

## Colors

A warm-neutral limewash room with walnut ink, one oil paint per room, and three semantic pens kept strictly apart from the paints.

### Primary
- **Walnut Ink** (`brand` / `ink`): the RAUM wordmark, primary buttons, the live mic orb, body text. Primary actions are ink on limewash, never a coloured brand fill.
- **Begleitstrich** (`strich`): the 4 px line where wall meets dado. In the dark theme it deepens to near-black so it still reads as a line.

### Secondary: the room paints
- **Resedagrün** (`paint-sprechzimmer`): the consultation room.
- **Ochsenblut** (`paint-kanzlei`): the law office.
- **Kobalt** (`paint-verhandlungsraum`): the hearing room, from the tiled stove.
- **Taubenblau** (`paint-gespraech`): the general conversation room; also the default `--paint`.
- **Off the record** (`paint-off`): the paint turns grey and the conversation fades to 45% greyscale.

`--paint` is set per screen by `.room--<mode>`, and `--them` (the other person's plate) is declared again in the same rule: a `var()` in a custom property resolves where it is declared, so on `:root` alone it stayed the default dove blue in every room. White text (`paint-ink`) holds at least 5:1 on every paint. On the paint, wells and rims are white at 14% and 42% (`paint-well`, `paint-rim`), and a 1 px white sheen (14%) runs along the paint's top edge.

### Tertiary: daylight
- **Window Daylight** (`sun`, an rgb triplet used only with alpha): the resting light patch on the hero sheet (a 112° gradient from 62% to 0), the Lichtfall sweep, text selection, and the daylight ring on the orb when a read-back waits for this device. The sun crossing the wall (`spectacle.css`) uses `--wall-sun-a` / `--wall-sun-b`: 0.55 / 0.18 in the morning, 0.10 / 0.04 in the evening theme, where a stronger patch dropped `ink-3` to 2:1.
- **Mullion Shadow** (`mullion`): the faint shadow of the window cross inside the light patch.

### Neutral
- **Kalkweiß** (`wall`): the page. Everything that is not a sheet sits directly on it.
- **Recessed Wall** (`wall-2`, `wall-3`): recessed fields, unselected options, notes on the wall, the "you" speaker plate, hover.
- **Cool Shade** (`shade`): where a draft waits; also the live-draft tag.
- **Sheet** (`sheet`, `sheet-2`): what text sits on longest; `sheet-2` for cells inside a sheet.
- **Ink steps** (`ink`, `ink-2` 8.8:1, `ink-3` 5.4:1 on the wall): final text, drafts and secondary text, labels and source lines.
- **Rules** (`rule`, `rule-strong`): hairlines between turns, header cells, sheet edges.
- **Link / Focus** (`link`, `focus`): cobalt; on the dado the focus outline turns white.

### Semantic pens
`ok` checked, `unsure` unsure / read-back / reconnecting, `bad` mismatch / destroy, `pending` checking, `live` the live dot. Each pen has a `-wash` (7–12% alpha) for its own chip, banner or hover only.

### Named Rules
**The Paint Below The Line Rule.** A room's paint appears only below a Begleitstrich, on the other person's speaker plate, the draft caret, and the selected radio square. It never fills a sheet or a text block on the wall.

**The Pens Are Not Paint Rule.** The semantic pens never act as room colour and a room paint never means a check state, even where hues sit close (reseda vs ok green, cobalt vs link).

**The Two Signals Rule.** Colour is never the only signal: every state also changes line type, glyph, and words.

## Typography

**Text Font:** Sofia Sans Variable (with Noto Sans Arabic Variable, system-ui)
**Plate Font:** Sofia Sans Extra Condensed Variable, uppercase
**Figure Font:** Sofia Sans Semi Condensed Variable, tabular figures

**Character:** one family in three widths. The normal width carries every sentence and label; the extra-condensed capitals are the enamel door plate (room names, the wordmark, sheet headings, the burn stamp); the semi-condensed width sets figures, ids and timings so numbers stay compact and tabular without a costume mono. All self-hosted via @fontsource.

### Hierarchy
- **Plate** (780, clamp(1.625rem → 2rem), line-height 0.9, uppercase, +0.012em): the room name in the header ("CONSULTATION ROOM"). Home doors 800 up to 2.25rem; sheet headings 1.375rem; Admin section heads 1.5rem.
- **Wordmark** (860, line-height 0.82, uppercase, +0.02em): RAUM on Join and Home.
- **Display / ramp-0** (720, clamp(1.75rem → 2.125rem), 1.18, −0.014em): the other person's newest sentence on the hero sheet.
- **Title / ramp-1** (620, 1.3125rem, 1.3): the previous turn, the read-back question.
- **ramp-2 / ramp-3** (1.125rem; 560, 1.0625rem, 1.38): older turns and your own turns (ink-2, 540).
- **Body** (450, 1rem, 1.5, `tnum` + `case`): everything else. Source lines 1.0625rem in ink-3.
- **Label** (650, 0.8125rem, +0.01em, sentence case): names the value next to it ("You speak", "Dose").
- **Figure** (760, 1.125rem, Semi Condensed): fact chip values, room ids, timings ("780 ms"). Arabic values fall back to the text face so they join.

### Named Rules
**The Recency Ramp Rule.** Hierarchy in the conversation is carried only by the ramp (34 → 21 → 18 → 17 px). Nothing else (colour, boxes, icons) ranks a turn.

**The Door Plate Rule.** Condensed uppercase is for names and figures that would be painted on a door: room names, the wordmark, sheet headings, the stamp. Labels stay sentence case in the text face.

## Layout

A 4 px base on an 8 px module (`s-1`…`s-8`: 4, 8, 12, 16, 24, 32, 48, 64); the Begleitstrich sits on it everywhere.

Phone (360–430 px), one column, three bands:
1. **Header on the wall** (not floating): row 1 is the room mark, the plate name, the German name and room id beneath, the link pill and the 44 px menu button. Short of room, the German name truncates; the room id never does. Row 2 is one hairline strip of cells between two `rule-strong` lines: `You speak · German` | `Patient speaks · Arabic` | `You hear on · Speaker`.
2. **Conversation on the wall**: turns separated by hairlines; only the other person's newest turn stands on the hero sheet (bleeding 8 px past the column). Banners are sheets pinned under the header, 16 px from the edges.
3. **Dado**: the room's paint below the Begleitstrich, a three-column grid (side action, mic orb, side action). Side actions are 46 px round wells with a sentence-case label under them, 76 px tall. The three labels share one line: the side wells stand on the orb's foot and the orb rises above them; the orb's caption sits under it as close as the side labels under their wells.

When a room opens, the Begleitstrich draws first (scaleX, 460–480 ms), then the paint rises below it (560 ms), then the dado's contents fade in.

Join (across the desk): the wordmark and the room's door plate on the wall, then one desk sheet split by one centre line: your seat (quiet plate, your language in its own script, how you hear, one Change) in the window's light, the other person's seat (painted plate, their language) opposite, and the box as a square walnut key on the line where the seats meet its promise, the consent one sentence per ruled line. Settled choices open in the desk behind the Change; the voice-copy choice always stays on screen. The action sits on the dado, with the AI disclosure ("This is an AI interpreter.") in words directly above the button, since on a short phone the dado covers the desk's promise until it is scrolled (EU AI Act Art. 50). Home: each room is a door, limewash above with its plate name and paint below the line (dado at 46%, 38% on wide screens). Table screen (laptop, read from 3 m): header on the wall, the conversation as one sheet split by one centre line with the two languages mirrored across it (each turn's fact chips stand in the cell of the language they were checked in, never across the line), the glass box as a hairline module grid, the paint along the floor; type scales from 16 to 22 px with the viewport, and the trust signals (check marks, fact values, the latency figures, the privacy-meter label) never go under 22 px. Admin: hairline-dense modules on the wall.

## Elevation & Depth

Depth has one rule: **the wall, a sheet on the wall, a sheet lifted for an interruption.** No blur, no translucency, no glow. A sheet is a solid fill with a hairline edge and one soft, warm offset shadow; a lifted sheet throws a longer one. The scrim behind a lifted sheet is walnut at 42%.

### Shadow Vocabulary
- **Sheet** (`box-shadow: 0 1px 0 rgb(33 27 23 / 0.05), 0 12px 28px -16px rgb(74 52 30 / 0.34)`): the hero sheet, banners, the selected option, sent notes.
- **Lift** (`box-shadow: 0 2px 4px rgb(33 27 23 / 0.06), 0 26px 48px -18px rgb(74 52 30 / 0.46)`): the read-back (it rises from sheet to lift in 420 ms) and the menu sheet.

Dark theme uses the same two steps in black (sheet `0 1px 0 rgb(0 0 0 / 0.25), 0 14px 30px -16px rgb(0 0 0 / 0.6)`).

### Named Rules
**The One Depth Rule.** Wall, sheet, lifted sheet. Nothing else casts a shadow; a draft has none at all (it is in the shade, not on a sheet).

**The No Sheet In A Sheet Rule.** A chip or a proof table may sit inside a sheet; a second shadowed sheet may not.

## Shapes

Sheets and controls share gently squared corners (6 px); chips, notes and inner parts 4 px; status, speaker plates, verify marks and fact notes are pills. Only round things are circles: the mic orb, the dado's icon wells, dots. The radio mark is a 12 px square with 2 px corners that fills with the room's paint. Lines are the main form device: the 4 px Begleitstrich, 1 px hairlines, and state lines in three types (solid, dashed, dotted). The room mark (`Hatch` in `Drawing.tsx`) is a swatch of the room's wall: limewash above, the paint below at 5/8 height, the Begleitstrich between, in a 2.5 px-cornered frame.

## Components

### Hero sheet and Lichtfall (the signature)
A sentence is **draft** while its words can still change (a partial caption, or a translation whose `final` is false) and **final** when the translation is committed. Turn.tsx: `const draft = live || (!!tr && !tr.final);` puts `is-draft` / `is-final` on `<article className="turn">` and `is-draft` on `.turn__main`.

| | Draft | Final |
|---|---|---|
| Classes | `.turn.is-draft`, `.turn__main.is-draft` | `.turn.is-final` (no class on `.turn__main`) |
| Hero sheet | cool shade fill, dashed edge (ink-3 at 30%), no shadow, no light | sheet fill, hairline edge, sheet shadow, resting daylight patch with the mullion shadow |
| Text | ink-2, dotted 2 px underline at 0.26em in ink-3 at 70% ("pencil") | ink; the underline retracts to transparent |
| Caret | 3 px bar in the room's paint, blinking | gone |
| Tag | verify mark `translating`: "Live draft", dotted pill on shade, breathing dot | Checking (dashed, spinning ring) → Checked (tick draws, 380 ms) / Unsure (dotted) / Mismatch (red, 2 px) |
| Motion | none while typing; turns never move | **Lichtfall**: fill, ink and underline change over 420 ms (`--t-clear`); one bright diagonal band (105°) sweeps across the sheet in 460 ms (`--t-sweep`, 60 ms delay); the light patch slides in and rests |

Under reduced motion it is instant. A corrected draft keeps the struck old text (red 2 px strike) above the new final text. Never blur draft text; the shade is on the sheet, not the words. A mismatch on the hero keeps the light and adds a red rim.

### Speaker plate
A pill at the start of every turn, 26 px, 760 weight label. The other person: painted in the room's colour, white text, a filled dot. You: a quiet `wall-3` plate, ink-2 text, a ring dot. While someone is speaking the dot breathes.

### Talk Control: the mic orb on the dado
`TalkControl` in `app/src/components/Sheet.tsx`. One 84 px orb on the dado, a label under it (`aria-live="polite"`), the voice level drawn as white rings (radius set inline, 90 ms linear). Classes: `.talk[data-mode][data-state][data-purpose]`.

```ts
mode?: "handsfree" | "hold"        // default "handsfree": tap toggles. "hold": push-to-talk
phase?: "idle" | "live" | "sending" | "blocked"   // derived from on/disabled/blocked when absent
blocked?: "other_speaking" | "voice_playing" | null
answering?: boolean                 // a read-back waits for this device's yes/no
onHoldStart?: () => void            // pointer down (captured) or Space/Enter down on the focused orb
onHoldEnd?: () => void              // pointer up / cancel, or Space/Enter up
onModeChange?: (m: "handsfree" | "hold") => void   // renders the Hands-free switch under the orb
```

| State | `data-state` | Orb | Label |
|---|---|---|---|
| Idle (hold) | `idle` | limewash orb, walnut mic, 6 px white halo | **Hold to talk**; desktop adds "On a keyboard, hold [Space]" (hidden on touch) |
| Held | `live` | walnut fill, white mic, scaled to 0.95 under the thumb, rings grow with the level | **Release to send** with a green breathing dot; `aria-pressed="true"`; 12 ms vibration on Android |
| Sending | `sending` | a white ring runs round the orb | **Sending…** |
| Blocked | `blocked` | flat white-14% fill, 2 px dashed white outline, no rings, `disabled` | **Wait: the other person is speaking** / **Wait: the translation is playing** |
| Read-back answer | `idle` + `data-purpose="answer"` | daylight ring (sun at 85%) | **Hold to answer** |
| Hands-free | `data-mode="handsfree"` | tap toggle; live = walnut | **Listening** / **Tap to speak**; the switch (`role="switch"`, `aria-checked`) flips modes |

Touch: `touch-action: none`, no callout or selection, pointer capture so a sliding thumb still releases. The orb plus its stage is a 100 px target. Errors keep "The microphone is blocked…".

### Fact chips
A small sheet pinned under the sentence (4 px corners): the value on top in the figure face (1.125rem, 760), its kind below (0.75rem label), a 3 px state line along its foot that draws from the start edge in 240 ms. **Solid green** = checked, **dashed** = checking, **dotted amber** = unsure. **Mismatch** (`.cloud`): the one chip that interrupts: 2 px red rim, red wash, said ≠ translated, the wrong value struck through, a small shake on entry (420 ms).

### Fact notes and read-back
- **Fact notes** (`.fnote`, the professional's phone): pills on the sheet. Confirmed = green rim and tick; unconfirmed = dashed amber rim, dotted ring and the word "unconfirmed"; answered no = red rim and cross.
- **Read-back** (`.rb`): never a modal; a sheet in the conversation lifted one step (the interruption depth). Unsure = 2 px dashed amber rim; mismatch = solid red rim. The question at ramp-1, max 34ch; Yes (green outline) and No (red outline) at 44 px; "Just say yes or no" when the box spoke it. While it is open the hero's fact chips step aside. Answered: it settles to one quiet line ("Confirmed · spoken").

### Header status pill
Outlined pill, 32 px, on the sheet fill. **Live** = green text and rim with a pulsing dot; **Reconnecting…** = amber with a spinning ring; **Offline** = red; off the record dims the plate and cells to 55%. On a phone, while the link is down (reconnecting or lost) the pill keeps only its mark, a 32 px round: the banner right under the header names the state in words, and the plate and room id keep their room.

### Banners
A sheet pinned under the header (6 px corners, sheet shadow) with a 9 px leading dot in its pen: neutral, warn (amber wash), bad (red wash), reconnect (spinning ring), off the record (recessed wall, no shadow). On a phone only one banner shows, the most urgent (Talk.tsx orders them: link, tap to hear, same language, off the record, voice playing, notes), so the conversation keeps its room. The conversation follows the newest sentence but never past the hero's top: the other person's newest sentence stays whole under a read-back or the reader's own reply, and a reader who scrolled away by hand is left there until they come back.

### Buttons
- **Shape:** gently squared (6 px), 52 px tall (60 px large, 44 px small).
- **Primary:** walnut fill, limewash text, 700 weight 1.0625rem; hover deepens to `brand-2`; press moves 1 px down.
- **Ghost:** sheet fill, `rule-strong` edge; hover to recessed wall.
- **On the dado:** limewash fill with walnut text (`.btn--wall`); hover to white.
- **Bad / ok-line:** sheet fill with a 1.5 px rim and text in the pen; hover adds the pen's wash.
- **Focus:** 2.5 px cobalt outline, 3 px offset; white on the dado.

### Segmented choice (audio route, Join)
Two 72 px tiles on the recessed wall (on Join, two full-width rows inside the desk's chooser) with a square radio mark. Selected: the tile becomes a sheet with a 2 px ink inset ring and the sheet shadow; the square fills with the room's paint.

### Summary and the burn
The summary is one sheet, its head the room's swatch: the walnut line on top, the paint under it. Each section heading is in the reader's language (English when the box has none), the language the items are read in beneath it; items are in the reader's language, or for a phone in neither conversation language the take-home person's, with the other language under them, never the same language twice. The take-home QR carries the reader's language (`?lang=`). The burn erases section by section and stamps "Key destroyed" in plate capitals, rotated −4° with a red double rim (460 ms).

### Motion
One easing, `cubic-bezier(0.22, 1, 0.36, 1)`. Quick state 160 ms; lines draw 240 ms; Lichtfall 420 ms with a 460 ms sweep: the light falls slower than anything else moves. Turns enter 8 px up in 300 ms and then never move. Sheets rise 28 px in 340 ms. Under `prefers-reduced-motion` every animation and transition is cut to 1 ms.

## Do's and Don'ts

### Do:
- **Do** put every action surface on the dado, below a 4 px walnut Begleitstrich, painted in the room's paint.
- **Do** keep text people read for long on a sheet (6 px corners, sheet shadow), and drafts in the shade with no shadow.
- **Do** mark draft → final with Lichtfall: 420 ms state change, one 460 ms daylight sweep, resting light.
- **Do** name every state in words beside its line type and colour.
- **Do** keep the chrome English and the content in each person's language, each content element with its own `lang` and `dir`.
- **Do** put the speaker plate on every turn so the reader always knows who spoke.
- **Do** set figures, ids and timings in Sofia Sans Semi Condensed with tabular figures.

### Don't:
- **Don't** use blur, translucency or glass; a sheet is solid.
- **Don't** add a depth beyond wall, sheet and lifted sheet, or nest a shadowed sheet inside another.
- **Don't** spread a room's paint above the line onto sheets or text blocks, or let a pen stand in for a paint.
- **Don't** blur or fade draft text for effect; legibility first.
- **Don't** move a turn once it is placed; only its words change.
- **Don't** use colour alone for checked / unsure / mismatch.
- **Don't** load fonts or assets from a third party; the privacy meter must stay at zero.
