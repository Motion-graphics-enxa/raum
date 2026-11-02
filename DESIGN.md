---
name: RAUM
description: Milchglas. The conversation behind frosted glass, in Berliner Blau. Translates. Checks. Forgets.
colors:
  field: "#e9f0f6"
  glass: "rgb(255 255 255 / 0.6)"
  glass-strong: "rgb(255 255 255 / 0.8)"
  glass-solid: "#f7fafc"
  glass-rim: "rgb(255 255 255 / 0.9)"
  glass-edge: "rgb(13 58 97 / 0.12)"
  frost: "rgb(255 255 255 / 0.42)"
  ink: "#0a1d30"
  ink-2: "#2b4157"
  ink-3: "#4b6177"
  rule: "rgb(13 58 97 / 0.12)"
  rule-strong: "rgb(13 58 97 / 0.3)"
  brand: "#0d3a61"
  brand-2: "#164f82"
  brand-ink: "#f2f8fd"
  link: "#145089"
  focus: "#1b6fd1"
  live: "#1f9d63"
  them: "#0d3a61"
  you: "rgb(13 58 97 / 0.08)"
  ok: "#12683f"
  unsure: "#8a4f00"
  bad: "#b3192a"
  pending: "#5a7085"
  room-sprechzimmer: "#0a6675"
  room-kanzlei: "#5945a4"
  room-verhandlungsraum: "#9c336b"
  field-dark: "#06182a"
  glass-dark: "rgb(20 48 76 / 0.5)"
  glass-strong-dark: "rgb(16 40 64 / 0.74)"
  glass-solid-dark: "#102b46"
  glass-rim-dark: "rgb(255 255 255 / 0.16)"
  ink-dark: "#eef5fb"
  ink-2-dark: "#c3d6e6"
  ink-3-dark: "#98b2c8"
  them-dark: "#2a6fae"
  link-dark: "#9fd3ff"
  ok-dark: "#6adba4"
  unsure-dark: "#f3bb62"
  bad-dark: "#ff8f8f"
typography:
  display:
    fontFamily: "Atkinson Hyperlegible Next Variable, Noto Sans Arabic Variable, system-ui, sans-serif"
    fontSize: "clamp(1.75rem, 1.1rem + 2.6vw, 2.125rem)"
    fontWeight: 640
    lineHeight: 1.2
    letterSpacing: "-0.012em"
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
  label:
    fontFamily: "Atkinson Hyperlegible Next Variable, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 650
    letterSpacing: "0.005em"
  figure:
    fontFamily: "Atkinson Hyperlegible Mono Variable, ui-monospace, monospace"
    fontSize: "0.8125rem"
    fontWeight: 560
  wordmark:
    fontFamily: "Atkinson Hyperlegible Next Variable, system-ui, sans-serif"
    fontSize: "clamp(2.75rem, 2rem + 4vw, 4.5rem)"
    fontWeight: 800
    letterSpacing: "0.08em"
rounded:
  control: "12px"
  chip: "14px"
  pane: "22px"
  dock: "28px"
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
components:
  pane:
    backgroundColor: "{colors.glass}"
    rounded: "{rounded.pane}"
    border: "1px {colors.glass-rim}, outer 1px {colors.glass-edge}"
    backdropFilter: "blur(22px) saturate(1.6)"
  button-primary:
    background: "linear-gradient(160deg, #1a5a92, #0d3a61 55%, #0a2e4f)"
    textColor: "{colors.brand-ink}"
    rounded: "{rounded.control}"
    height: "52px"
  button-ghost:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
  talk-orb:
    size: "92px"
    backgroundColor: "{colors.glass-strong}"
    textColor: "{colors.brand}"
    rounded: "{rounded.round}"
  talk-orb-live:
    background: "{components.button-primary.background}"
    textColor: "#ffffff"
  header:
    backgroundColor: "{colors.glass-strong}"
    rounded: "{rounded.pane}"
  speaker-plate-them:
    backgroundColor: "{colors.them}"
    textColor: "#ffffff"
    rounded: "{rounded.pill}"
  speaker-plate-you:
    backgroundColor: "{colors.you}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.pill}"
  fact-chip:
    backgroundColor: "{colors.glass-strong}"
    rounded: "{rounded.chip}"
  verify-pill:
    rounded: "{rounded.pill}"
    height: "30px"
---

# Design System: RAUM

## Overview

**Creative North Star: "Milchglas"**

The frosted glass door of a Berlin practice or law office: from the corridor you can see that a conversation is happening, never what is said. RAUM's screens are panes of that glass over a soft field of Berliner Blau light. The interface is the door; the words are the room. The signature: **a draft sits behind the frost, and when the sentence is final the glass clears.**

Calm, quiet, trustworthy. One brand colour (Berliner Blau), three semantic pens (green verified, ochre unsure, red mismatch), one room tint in the field. No chat bubbles, no avatars, no glowing AI badge.

**All UI chrome is English** on every device (`UI_LANG = "en"` in `app/src/lib/i18n.ts`; `t()` and `pick()` read it whatever the phone's language). Content stays in each person's language: what they said, its translation, the read-back question. The language picker names each language in its own script with the English name beside it ("العربية · Arabic"). The chrome is always left to right; every sentence carries its own `lang` and `dir`, so Arabic content still runs right to left.

**Key characteristics**
- Frosted panes (translucent fill, 22 px blur, 1 px light rim, inner top highlight, soft offset shadow) over a fixed light field (`body::before`).
- Every pane has a solid fallback: without `backdrop-filter`, or with `prefers-reduced-transparency`, `--glass` becomes `--glass-solid`, so text contrast never depends on the blur. All text passes WCAG AA on the solid fallback and on the translucent pane over the field.
- State is drawn twice: line type first (solid, dashed, dotted), colour second, and named in words.
- One recency ramp carries hierarchy (34, 21, 18, 16 px). Only the newest sentence of the other person sits on its own pane; older turns lie on the field.

## Colors

- **Field** (`--field`): the room behind the glass. Light: mist (#e9f0f6) with a Berliner Blau glow top-left and the room's tint bottom-right. Dark: Prussian night (#06182a) with stronger glows. The room tint is `--room-rgb`, set by `.room--<mode>` on the screen.
- **Glass** (`--glass`, `--glass-strong`, `--glass-rim`, `--glass-edge`, `--glass-hi`): panes. `glass-strong` for what text sits on longest (header, dock, sheets, banners).
- **Frost** (`--frost`): the extra frost a draft sits behind.
- **Berliner Blau** (`--brand`, `--brand-2`, `--brand-grad`): primary buttons, the live mic orb, the selected language chip, the other person's speaker plate (`--them`).
- **Ink** (`--ink`, `--ink-2`, `--ink-3`): three text strengths, AA on every pane in both themes (ink-3 ≥ 5.5:1, ink-2 ≥ 9:1 on the lightest pane).
- **Semantic pens**: `--ok` verified, `--unsure` unsure / read-back, `--bad` mismatch / destroy, `--pending` checking, `--live` the live dot. Each has a `-wash` for its own chip or banner only.

**Rules.** The brand never means a check state; a pen never acts as the brand. Colour is never the only signal.

## Typography

Atkinson Hyperlegible Next for all words and labels (self-hosted via @fontsource). Labels are sentence case, 13 px, 650 ("You speak", "Patient speaks", "Dose"); no uppercase tracked mono lettering. Atkinson Hyperlegible Mono only for ids and timings (room id, "780 ms"). Fact values use the word face with tabular figures, so Arabic values join correctly. Arabic, Persian and Urdu get zero letter-spacing and a 1.6 line height.

## Layout

Phone (360–430 px), one column:
1. **Header pane** (floating, 12 px from the edges). Row 1: room icon, English room name ("Consultation room"), German name and room id beneath ("Sprechzimmer · SZ-7921"), the link pill, the menu. Row 2: **who speaks what, and how you hear it**: `You speak · German` → `Patient speaks · Arabic`, then `You hear on · Speaker` (or Headphones) with its icon.
2. **Banners** as glass toasts under the header (role=status / alert unchanged).
3. **Conversation**: hero pane for the other person's newest sentence; earlier turns on the field.
4. **Dock**: one glass bar (28 px corners): side action, mic orb, side action. Side actions are 44 px round icon wells with a sentence-case label under them.

Join: the RAUM wordmark and room on a header pane; each decision (language, audio route, consent) is its own pane; the join button sits on a sticky glass bar. Summary: one large pane. Table screen: header pane plus two panes (conversation, glass box) with 12 px gutters. Admin: panes for the meter band, new room, each room.

## Elevation & Depth

Depth comes from the glass: fill, rim, inner highlight and `--shadow-pane` (0 14px 36px -12px). `--shadow-float` for the dock, the mic orb and bottom sheets. Scrims are Prussian with a 6 px blur.

## Shapes

Controls 12 px, chips 14 px, panes 22 px, dock and sheets 28 px, pills (status, plates, verify mark, fact notes, language chips) fully round. Only round things are circles: the mic orb, dots.

## Components

### Speaker plate (who is speaking)
`.turn__who`: a pill at the start of every turn. The other person: a Berliner Blau plate with white text and a dot ("• Patient"). You: a quiet glass plate ("• You said"). While they are speaking (`.turn.is-live`) the dot breathes.

### Link state pill (header)
`.tblock__status`: **Live** = green pill with a pulsing dot (`.is-live`); **Reconnecting…** = ochre pill with a spinning ring (`.is-reconnecting`); **Offline** = red (`.is-lost`); off the record = grey header. The reconnecting banner (`.banner--reconnect`) repeats it in words under the header after 1.5 s.

### Audio route (headphones vs phone speaker)
Header cell 3, `.tblock__cell--end`: "You hear on" + headphones or speaker icon + the word. Chosen on Join in a two-option segmented control (`.seg`); the selected option is a raised glass tile with a Berliner Blau rim.

### Draft and Final (the signature; spec for the live-draft feature)
A sentence is **draft** while its words can still change: a partial caption (`turn.partial` with no final text) or a translation whose `final` is false. It becomes **final** when the translation is committed.

| | Draft | Final |
|---|---|---|
| Classes | `.turn.is-draft`, `.turn__main.is-draft` | `.turn.is-final` (no class on `.turn__main`) |
| Text colour | `--ink-2` (9.6:1 light, ≥ 9:1 dark) | `--ink` |
| Underline | dotted 2 px, offset 0.24em, `--ink-3` at 70% ("pencil, not ink") | none (transparent) |
| Caret | 3 px Berliner Blau bar at the end, blinking | gone |
| Hero pane | frosted a step more: `--frost` mixed in, inner 1.5 px frost ring | clear glass |
| Tag (gutter) | verify mark in state `translating`, labelled **"Live draft"**: dotted pill, breathing dot | verify mark: Checking (dashed) → Checked / Unsure / Mismatch |
| Motion | none while typing | colour and underline fade out in 280 ms ease-out (`--t-clear`); under reduced motion it is instant |

How to use it (Turn.tsx already does): `const draft = live || (!!tr && !tr.final);` then put `is-draft` / `is-final` on the `<article className="turn">` and `is-draft` on `.turn__main`. A draft that gets corrected keeps the struck-through old text (`.turn__struck`) above the new final text. Never blur the draft text itself: legibility first; the frost is on the pane, not the words.

### Fact chips (doses, numbers, dates)
`.dim` inside `.facts`: a glass chip, value on top (17 px, 700), its kind below ("Dose"), a 2.5 px state line along the chip's foot. **Solid green** = verified, **dashed slate** = checking, **dotted ochre** = unsure. The line draws from the start edge when the check lands (220 ms). **Mismatch** (`.cloud`): the one chip that interrupts: 2 px red rim, red wash, what was said ≠ what was translated, the wrong value struck through.

### Noted and read-back states
- **Fact notes** on the professional's phone (`.fnote`): pills. Noted / confirmed = solid green rim + check; unconfirmed = dashed ochre rim + dotted ring + the word "unconfirmed"; answered no = red rim + cross.
- **Read-back** (`.rb`): a glass note in the conversation (never modal). Unsure = dashed ochre rim; mismatch = solid red rim; the question at 21 px; Yes (green outline) and No (red outline) buttons, 44 px; "Just say yes or no" hint when the box spoke it. Answered: settles to one quiet line ("Confirmed ✓ · spoken").

### Talk Control: the mic orb, hands-free and push-to-talk
`TalkControl` in `app/src/components/Sheet.tsx`. One 92 px orb in the dock, a label under it (`aria-live="polite"`), the level drawn as three soft halos (radius from `level`, 90 ms linear). Classes: `.talk[data-mode][data-state][data-purpose]`.

**Props (all optional beyond the original five, so today's hands-free behaviour is unchanged):**

```ts
mode?: "handsfree" | "hold"        // default "handsfree": tap toggles (today). "hold": push-to-talk
phase?: "idle" | "live" | "sending" | "blocked"   // derived from on/disabled/blocked when absent
blocked?: "other_speaking" | "voice_playing" | null
answering?: boolean                 // a read-back waits for this device's yes/no
onHoldStart?: () => void            // pointer down (captured) or Space/Enter down on the focused orb
onHoldEnd?: () => void              // pointer up / cancel, or Space/Enter up
onModeChange?: (m: "handsfree" | "hold") => void   // renders the Hands-free switch under the orb
```

| State | `data-state` | Orb | Label | Notes |
|---|---|---|---|---|
| Idle (hold) | `idle` | glass orb, Berliner Blau mic-off glyph, faint halo | **Hold to talk** | desktop shows "On a keyboard, hold [Space]" (`.talk__hint--keys`, hidden on touch) |
| Held | `live` | Berliner Blau gradient fill, white mic, scaled to 0.95 under the thumb, halos grow with the level | **Release to send** with a green live dot | `aria-pressed="true"`; Android gets a 12 ms vibration on press (iOS has no web vibration) |
| Released / processing | `sending` | glass orb, a ring runs round it | **Sending…** | until the transcript arrives |
| Blocked | `blocked` | flat, dashed rim, no halo, `disabled` | **Wait: the other person is speaking** / **Wait: the translation is playing** | `blocked` = `other_speaking` / `voice_playing` |
| Read-back answer | `idle` + `data-purpose="answer"` | ochre halo ring | **Hold to answer** | when a read-back for this device is open |
| Hands-free | `data-mode="handsfree"` | today's tap toggle; live = gradient fill | **Listening** / **Tap to speak** | the switch (`role="switch"`, `aria-checked`) flips modes |

Touch: `touch-action: none` and no callout or selection on the held orb, pointer capture so a sliding thumb still releases correctly. The orb plus its stage is a 108 px target, centred in the dock for either thumb. Errors keep the existing "The microphone is blocked…" label.

### Banners
Glass toasts with a leading dot in the tone: neutral, warn (ochre), bad (red), reconnect (spinning ring), off the record (grey).

### Buttons
Primary: Berliner Blau gradient, 52 px, 12 px corners, inner highlight, soft blue shadow; hover brightens. Ghost: glass. Bad / ok-line: glass with a 1.5 px rim in the pen.

### Summary and the burn
The summary is one glass pane. The burn erases section by section and stamps "Key destroyed" (unchanged behaviour and markup).

### Motion
One easing, cubic-bezier(0.16, 1, 0.3, 1). Turns enter 6 px up in 240 ms; sheets rise in 280 ms; the frost clears in 280 ms; the live dot pulses; the state line draws in 220 ms. Under `prefers-reduced-motion` every animation and transition is cut to 1 ms.

## Do's and Don'ts

**Do**
- Put text on `--glass-strong` when it is read for long; keep the solid fallback.
- Name every state in words beside its line type and colour.
- Keep the chrome English and the content in each person's language; give every content element its own `lang` and `dir`.
- Use the speaker plate on every turn so the reader always knows who spoke.

**Don't**
- Blur text, or put body text directly on the field without a pane in dark mode.
- Stack panes inside panes (one level: a chip inside a pane is fine, a pane inside a pane is not).
- Use colour alone for verified / unsure / mismatch.
- Load fonts or assets from a third party; the privacy meter must stay at zero.
