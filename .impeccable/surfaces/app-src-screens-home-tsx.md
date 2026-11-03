---
version: 1
slug: "app-src-screens-home-tsx"
primary_target: "app/src/screens/Home.tsx"
related_targets: ["app/src/components/QR.tsx","app/src/screens/Admin.tsx","app/src/screens/Table.tsx"]
---

## Scope

Start page `/start` (Operate): the room picker and the hand-over of the codes once a room is open, plus the codes wherever they are shown (Admin `RoomCodes`, the Table screen's join block, the shared `QR`). Inside the established Altbau Daylight world (DESIGN.md); shared tokens and other screens stay as they are. Request (3 Nov 2026): "a better, more premium experience" for the picker and the QR presentation. Functional rules: the codes still encode `/j/:room?role=a|b` (Join reads room, role and lang from the URL; lang falls back to the phone), the Table code `/t/:room`; dark theme keeps WCAG AA.

## Shape brief

1. **Job.** Whoever stands at the box (the professional on a laptop or tablet, or on their own phone) picks the room for this conversation, then hands each person their own code. Each phone scans and joins in seconds; the table screen opens on the monitor.
2. **Outcome and proof.** The room is open and both codes are in reach at once, each labelled with who scans it; the operator sees each person arrive without asking.
3. **States.** rooms loading, box not reachable (retry), no rooms on the box, opening a room (that door busy, others still), open failed, pass for an unknown room, pass: waiting for both / one joined / everyone in; events link down; copy done / copy failed.
4. **Ruled out.** Codes below the fold after a click; three equal codes with the table competing with the people; a code that inverts in the dark theme; a modal.

## Ordered structures (surface roll, operate)

1. The chosen door widens in place into the hand-over; the others fold to jambs.
2. Doorbell panel: compact plate rows, the hand-over replaces the list.
3. Rail and stage: the rooms on a rail, the stage shows the chosen room, then its codes.
4. Bilateral desk: the hand-over split by the centre line, each code facing its person.
5. The room pass: the doors, then one issued pass with a coupon per person and a table stub, live state on each coupon.
6. Stepper: choose, scan, open the table, as numbered steps.
7. Codes first: a room pre-opened with room tabs above.

Dealt 5, 3, 4; 5 leads. The decision page could not reach the user from this delegated run (no question channel in the subagent harness), so the dealt lead was built. Challengers: jet-age ticket wallet competitive (donates: a coupon's state stamps itself onto it and nothing disappears); split-flap declined (donates: states change in place, the coupon never moves); Crouwel grid, convention catalog, metro tiles, timetable rack declined.

## Direction contract

THESIS: Opening a room issues its pass: one sheet that is the opened door, the people's codes on its wall, the room's paint and actions below the line. It refuses the grid of equal QR tiles under a picker and the tall empty doors that push the codes off screen.

OWN-WORLD: Altbau Daylight as DESIGN.md records it. Doors: limewash above with the German plate, the English name, roles and what is checked; the paint below the Begleitstrich carries "Open room". Pass: sheet with the plate and room number in figures; coupons carry the speaker plates (quiet for the professional, painted for the other person), a code always walnut on paper in both themes with the room's swatch at its centre, a 3 px state line at the foot (dashed waiting, solid green joined); the dado holds the table action.

STORY: The operator reads "Open a room", picks a door, sees the pass rise with each person's code, watches each coupon turn to "Joined · Russian", opens the table screen.

FIRST VIEWPORT: Desktop 1440×900: wordmark and Admin on the wall, "Open a room" with one line, four doors in a row fully in view. After a click: back link, the pass at up to 1120 px: plate name left, room number right, coupons a | b | table, dado under them. Phone 390: doors stacked as short rows; the pass shows one code at a time behind a three-way switch.

FORM: The room pass, candidate 5 of 7 on the ordered list, dealt lead. seed:1610641ed5dbc33a. Signature: the pass is issued as a room opens: Lichtfall sweeps the sheet, the code prints in, the Begleitstrich draws and the paint rises; each coupon's foot line turns solid green as its phone joins.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Picker v2 (3 Nov 2026, user: the four door cards look like "just 4 blocks"; a fully different style)

Ordered structures: 1 floor plan of the practice, rooms as plan areas; 2 doorbell panel of engraved plates; 3 monumental index, one full-width row per room; 4 drenched stage, one room at a time in its paint; 5 key board with hanging key tags; 6 corridor in perspective; 7 expanding colour fields. Roll seed:340d266677767bfc dealt 3, 2, 6; 3 leads and is built here (two other agents build other concepts in parallel). Challengers fused: split-flap (rows are entities, states change in place), streaming wall (the focused row lifts, the rest dims), metro tiles (type is the interface, colour fields as content); orienteering, zoo map, ticket wallet declined.

### Direction contract (picker v2)

THESIS: The picker is the building's register: four room names set monumental, one full-width row each, and reaching for one paints that row as its room. It refuses cards, tiles and any grid of equal blocks.

OWN-WORLD: Altbau Daylight's materials used as typography: Sofia Sans Extra Condensed caps at poster size in walnut on limewash, hairline rules between rows, the room mark and prefix code in figures at the start, the English name, roles and checks small at the end. Hover or focus draws the 4 px Begleitstrich across the row and raises the room's oil paint under it; the name turns white and tracks in.

STORY: The operator reads four names at a glance, moves toward one, sees it become that room's paint while the others step back, and opens it.

FIRST VIEWPORT: Desktop 1440×900: bar, "Open a room" and one line, a register head, four rows of ~90 px names filling the width, all in view. Phone 390: four rows, mark and English name above each name set to the full width, details under it.

FORM: Monumental index, candidate 3 of 7, dealt lead. seed:340d266677767bfc. Signature: Paint under the line (strich draws 300 ms, paint rises 420 ms, the rest of the register dims).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
