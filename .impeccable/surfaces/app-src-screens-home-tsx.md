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
