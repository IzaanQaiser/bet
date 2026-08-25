# Hero Section — Implementation Spec

The landing page hero. One orchestrated, four-beat animation carries the entire product
argument: a text becomes a real Google Calendar event, a casually-mentioned idea gets held
without being scheduled, and then — days later, unprompted — the system finds free time and
brings that idea back, and separately reminds about the thing it already scheduled. Design
locked in via iterative mockup review; this spec is the resulting source of truth, superseding
an earlier two-beat draft.

**Build as real DOM/React components.** Not a video, webp, or Lottie. The copy in these bubbles
*is* the pitch; it needs to be editable, selectable, and crisp at any width.

**Stack:** Next.js App Router (`output: 'export'` — this ships to GitHub Pages, a static host,
so no server components/actions in this section), Tailwind, shadcn/ui (neutral base), Framer
Motion.

A working reference implementation (vanilla HTML/CSS/JS, built during design review) exists —
match its exact behavior; this spec documents what it does, don't reinterpret it.

---

## 1. Layout

```
┌───────────────────────────────┬───────────────────────────────┐
│  BET                          │  ┌─────────────────────────┐  │
│                               │  │  Monday September 7th    │  │ ← day badge, sticky
│  Nothing to remember.         │  └─────────────────────────┘  │   within its own group
│  Just text it.                │                               │
│                               │              ┌──────────────┐ │
│  It listens and quietly       │              │ user message │ │
│  keeps track of everything    │              └──────────────┘ │
│  else, including when         │  ┌────────────────┐           │
│  you're actually free         │  │ agent reply    │           │
│  enough to do it.             │  └────────────────┘           │
│                               │  4:41 PM                      │
│  Get started                  │              ┌─────┐          │
│                               │              │aight│          │
│  ┌─────────────────────────┐ │              └─────┘          │
│  │ ⋯ AGENT MEMORY          │ │  ┌──────────────────────────┐ │
│  │ remembered so you don't │ │  │ 📅 Added to Google Cal.  │ │
│  │ have to                 │ │  │ S M T W T [F] S          │ │
│  │ ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈ │ │  │ Buy birthday gift...     │ │
│  │ buy birthday gift—sarah │ │  │ 12:00 PM                 │ │
│  │                     fri │ │  └──────────────────────────┘ │
│  └─────────────────────────┘ │                               │
└───────────────────────────────┴───────────────────────────────┘
```

- Two columns, `1fr 1.1fr`, gap `56px`. No card, no shadow, no rounded container around
  either column — the columns themselves have no border.
- **Left column, top to bottom, one aligned stack, same left edge throughout:** eyebrow → h1 →
  lead → CTA → agent memory panel. The memory panel is *not* a separate floating element; it's
  part of this same column, directly below the CTA.
- **Right column:** a single fixed-height, vertically-scrolling viewport containing the
  conversation. Nothing else lives in this column.
- **Both columns are pinned to a fixed height** (`min(640px, 70vh)`) — not `max-height`, an
  actual `height`. This is load-bearing, not a style preference: earlier drafts used
  `max-height` and the box kept growing with content, which shifted the whole page's vertical
  centering every time a new message or memory row appeared. A real, unconditional `height`
  stops that at the source, for both the hero row itself and the thread viewport inside it.
- The whole hero is vertically centered in the viewport (`min-height: 100vh` on the page wrapper,
  `justify-content: center`) — it doesn't sit pinned to the top with dead space below it.
- Max width `1200px`, centered.

### 1.1 Copy

| Element | Text |
|---|---|
| Eyebrow | `bet` (renders uppercase via CSS `text-transform`, source stays lowercase) |
| Headline | `Nothing to remember. Just text it.` (hard break after "remember.") |
| Subhead | `It listens and quietly keeps track of everything else, including when you're actually free enough to do it.` |
| CTA | `Get started` |

CTA is a text link with a `--foreground` underline offset 6px, not a filled button — a filled
button competes with the dark sent-bubbles for the eye, and there's only one action on this page.

### 1.2 Agent memory panel

Sits below the CTA, in the same column. This is **not framed as a UI the user opens** — it's a
window into what the agent is quietly holding onto in the background, not a to-do list.

- Container: `1.5px dashed` border (never solid) — the dashed line itself is part of the
  "background/agent-only, not a front-end surface" signal. Transparent background, `10px`
  border-radius.
- Header: eyebrow-style label `AGENT MEMORY` (Geist Mono, same treatment as the page eyebrow)
  with a small `5px` pulsing dot beside it (`opacity 0.35 → 1`, `2.4s` ease-in-out loop — a
  quiet "still watching" cue), then a one-line subhead: `remembered so you don't have to`.
- Rows: Geist Mono, `12px`, title left / date-or-status right, `1px dashed` rule between rows
  (not solid — matches the container's own dashed language).
- **The panel's own height grows as rows are added — animated, not a sudden snap.** Each row is
  wrapped in its own `display: grid; grid-template-rows: 0fr` container that transitions to
  `1fr` over `480ms` (`cubic-bezier(0.4, 0, 0.2, 1)`) when it should appear, with the row itself
  inside an `overflow: hidden` inner wrapper. This is the CSS grid technique for animating to an
  unknown content height — do not attempt this with `max-height` guessing.
- **Retrieved state:** when an item gets surfaced back to the user later (beat 3/4 below), its
  row gets a red strikethrough that draws across the **entire row** — through the date/status
  text on the right, not just the title — and the title text dims to `--muted-foreground`. This
  state is permanent for the rest of that loop (not a flash): once retrieved, it stays struck
  through. Implementation: the strikethrough line is a `::after` pseudo-element on the row itself
  (`position: relative`), not on the title span, specifically so it can span the row's full
  width including the right-aligned date/status column. `520ms`
  `cubic-bezier(0.65, 0, 0.35, 1)`, `scaleX(0) → scaleX(1)`, `transform-origin: left`.
- Red is a deliberate, singular exception to the zero-accent rule (§8) — a genuine semantic
  "this is done" signal, not a decorative hue. Nothing else on the page uses it.

---

## 2. Type

Stock shadcn everything, with **one** deviation — same rule as before, still exactly one.

| Role | Face |
|---|---|
| Headline (`h1` only) | Instrument Serif, regular, `-0.02em`, `1.05` line-height |
| Everything else | Geist Sans |
| Timestamps, day badges, calendar-card labels, memory-panel rows | Geist Mono |

Do not extend the serif to `h2`, the eyebrow, the CTA, or anywhere in the memory panel or
calendar card — all of those stay disciplined so the one serif moment reads as intentional.

Eyebrow: Geist Mono, `0.75rem`, `0.12em` letter-spacing, uppercase, `--muted-foreground`.

---

## 3. Bubbles

Unchanged from the original spec — standard messaging conventions, getting these backwards reads
as uncanny to anyone who owns a phone.

| | User (sent) | Agent (received) |
|---|---|---|
| Alignment | right | left |
| Background | `--primary` | `--muted` |
| Text | `--primary-foreground` | `--foreground` |
| `transform-origin` | `bottom right` | `bottom left` |
| Max width | 82% of column | 82% of column |
| Padding | `9px 13px` | `9px 13px` |
| Radius | `18px`, bottom-right `4px` | `18px`, bottom-left `4px` |

Gap between consecutive same-speaker bubbles: `9px`. Between speaker changes: `16px`.

One timestamp only, under the agent's first reply in beat 1 (`4:41 PM`, Geist Mono, `0.6875rem`,
`--muted-foreground`, fades in `200ms` after its bubble settles). No timestamp anywhere else —
one is a detail, more is clutter.

**Never a raw hex, px value, or font-family in component code** — everything resolves through
Tailwind classes backed by the shadcn CSS variables. Two narrow, deliberate exceptions: the
Google Calendar icon's own brand colors (§4), and the retrieved-state red (§1.2) — both real
semantic/brand colors, not decorative accents, and both live as their own named tokens
(`--retrieved`, the icon's inline SVG fills), never inlined ad hoc elsewhere.

---

## 4. Calendar card

New component, not in the original spec — this is what makes "it actually writes to your real
calendar" legible instead of just claimed in a text bubble.

- A bordered card (`1px solid --border`, `10px` radius, `--background` fill), max-width 82% of
  the column, left-aligned like a received bubble.
- **Header row:** a small Google Calendar icon (a real, recognizable rendition — the four-color
  corner treatment plus a blue date number, not a generic calendar glyph) + a label in Geist
  Mono, `10px`, `--muted-foreground`, background `--muted / 0.5`. Label text varies by moment:
  `Added to Google Calendar` (just created), `Free on your Google Calendar` (an open-time
  suggestion), `On your Google Calendar` (a later reference to something already scheduled).
- **Day strip:** `S M T W T F S`, Geist Mono `10px`, the relevant day bold + `--foreground` with
  a `2px` bottom rule; the rest `--muted-foreground`.
- **Body:** a `3px` vertical bar (solid `--foreground` for a real/booked event; `1.5px dashed
  --muted-foreground`, no fill, for an "open time" card) beside the title (`13px`, `500` weight —
  italic + `--muted-foreground` specifically for the open-time variant) and time (Geist Mono,
  `11px`, `--muted-foreground`).
- Entrance: `translateY(8px) → 0`, `opacity 0 → 1`, `420ms ease-out` — same family as the memory
  row's own entrance, not the bubble spring (this is a card, not a message).
- `role="img"` with a full descriptive `aria-label` (e.g. "Google Calendar — Buy birthday gift
  for Sarah, 12:00 PM") — screen readers get the card's meaning in one string, not a
  component-by-component read-through.

### 4.1 The "just landed" glow

A temporary, neutral emphasis pulse — never a new accent hue, the same `--foreground`-tinted
ring/wash used for both the calendar card and its paired memory-panel row, fired **on the same
tick** so the two feel like one event, not two:

- Calendar card: a box-shadow ring, `0 0 0 0 → 0 0 0 9px` spread with a `22px` blur fading
  alongside it, `1100ms ease-out`, plays once.
- Memory row: a background-color wash, `--foreground / 0.1 → 0`, `1100ms ease-out`, plays once.
- **Sync mechanism:** the function that creates a calendar card returns the card element instead
  of glowing itself. The function that reveals a memory row accepts that element (plus its own
  row) and fires both glows from a single `setTimeout` callback, timed to land ~450ms after the
  row's own grid-reveal transition starts (so the glow reads as "this settled in, and now it's
  emphasized," not simultaneous with the reveal itself).
- Only fires where something was actually just created (beat 1's gift card + row, beat 2's idea
  row). Beat 3's open-time card and beat 4's reference card are not "just created" moments, so
  they don't glow.

---

## 5. Day groups — how a new day replaces the last

Each day's entire exchange (its badge + every bubble/card that follows) lives inside one
`.day-group` wrapper. **Only one `.day-group` ever exists in the thread at a time.**

This is a structural decision, not a scroll-position one — two earlier approaches (aligning a
`position: sticky` badge to the top of the viewport, then a scroll-floor that capped how far
back the view could scroll) both failed for the same reason: a single day's own content can be
shorter than the fixed viewport height, so there's nothing to scroll past regardless of what
target is computed. Removing the previous day from the DOM entirely is what actually guarantees
"only one day visible," independent of how much or little that day's content adds up to.

Sequence when a new day starts:
1. If a `.day-group` already exists, it plays a leave transition — `translateY(-48px)`,
   `opacity → 0`, `420ms cubic-bezier(0.4, 0, 1, 1)` — then is removed from the DOM once that
   transition completes (`await`ed, not fire-and-forget).
2. A new `.day-group` is created, containing just its day badge (a solid pill — see below —
   `position: sticky; top: 0` within the group, relevant if a single day's own content ever
   grows past the viewport height).
3. Every subsequent bubble/typing-indicator/calendar-card for that day appends into this new
   group.
4. The viewport auto-scrolls to reveal new content within the current group as it's added
   (`scrollTo(scrollHeight - clientHeight)`) — trivially safe now, since only one day's content
   can ever be in the DOM to scroll through.

### 5.1 Day badge

Replaces the original spec's hairline "───── FRI 28 AUG ─────" divider entirely — that read as
too subtle once real content (calendar cards, a growing memory panel) surrounded it. Now a solid
pill: Geist Mono `0.6875rem`, `0.08em` letter-spacing, `--muted` background, `1px solid --border`,
`999px` radius, full date in the format `Wednesday September 9th` (not abbreviated). Fades in +
`translateY(-6px) → 0` over `380ms`. A visually-hidden full date (`Wednesday, September 9th`)
duplicates the visible text for screen readers, matching the visible string closely enough that
nothing is lost, not abbreviated the way the old divider's `FRI 28 AUG` format was.

---

## 6. Motion

### 6.1 Bubble entrance

iMessage bubbles don't slide or fade — they **pop**, with overshoot, anchored to the corner they
came from.

```
from:  opacity 0, scale 0.62, translateY 14px
to:    opacity 1, scale 1,    translateY 0
```

- **Framer Motion:** `{ type: "spring", stiffness: 500, damping: 32, mass: 0.9 }`
- **CSS fallback:** `380ms cubic-bezier(0.34, 1.56, 0.64, 1)`

Opacity resolves faster than transform — ramp it over the first ~120ms so the bubble is fully
visible while still springing. Transform and opacity only — never `width`, `height`, `top`, or
`left`.

### 6.2 Typing indicator

Bubble enters with the received-bubble spring. Inside, three dots: `6px` diameter,
`--muted-foreground`, `5px` gap, each `scale 0.75 → 1.0` / `opacity 0.4 → 1.0`, `1200ms` loop
`ease-in-out`, `160ms` stagger between dots.

On exit, cross-fade over `140ms` while the real bubble springs in from the same origin — the
typing bubble should appear to *become* the message, not fade out separately from a second fade-in.

**Beats 3 and 4's opening line gets no typing indicator** — a typing indicator means "I'm
responding to you"; these are unprompted. That absence is the point.

### 6.3 Timeline

Trigger once on intersection at ~30% visibility, not on page load. Approximate timings (tuned
during design review — treat as the target, not a rigid spec to the millisecond):

| Beat | Flow |
|---|---|
| **1 — capture, proven against a real calendar** | Day badge: `Monday September 7th`. User: `yo i need to get sarah a gift for her birthday`. Typing → agent: `bet, i'll set a reminder for friday at noon` (timestamp `4:41 PM`). User: `aight`. → Calendar card (booked, Friday, "Buy birthday gift for Sarah", 12:00 PM, "Added to Google Calendar"). → Memory row inserts ("buy birthday gift — sarah / fri"), synced glow with the card. |
| **2 — a "someday" idea, held without scheduling** | Day badge: `Wednesday September 9th`. User states a real, specific idea (not a vague placeholder — see §6.4). Typing → agent: `bet, i'll keep that in mind` — no calendar card, this is a latent, nothing scheduled. → Memory row inserts, glows alone (nothing to sync with). |
| **3 — speaks first, ties back to what it's holding** | Day badge: `Thursday September 10th`. Agent (no typing): references the idea by name, ties it to found free time. → That memory row's retrieved state fires (strikethrough). → Calendar card (open variant, "Free on your Google Calendar"). → User replies in the affirmative — this is a real exchange, not the bot talking alone. |
| **4 — the day-of reminder, closing the loop back to beat 1** | Day badge: `Friday September 11th`. Agent (no typing): reminds about beat 1's item, by name. → That row's retrieved state fires. → Calendar card (booked variant again, "On your Google Calendar" — same event, re-surfaced). → User replies, closing the beat. |

Hold ~4s after the last event, then reset instantly (no fade) and replay — only while the
section is in the viewport (pause on scroll-away, an `IntersectionObserver`, not a scroll
listener).

### 6.4 The hackathon idea must be real

Beat 2's user line is a genuine, specific, short-but-complete pitch — not "I have an idea." The
design-review reference: *"yo i wanna build an agent crew for the agentic ai hackathon — one
that triages prod incidents, reads logs, checks recent deploys, drafts the postmortem."* The
memory-panel row then carries a short version of the same idea (`incident-triage agent crew`),
and beat 3's callback references it by that same name — the specificity has to survive all three
touchpoints, not just the opening line.

### 6.5 No celebration on either payoff

No checkmark, no confetti, no scale-bounce, no color shift beyond the two deliberate exceptions
in §1.2/§8. The product's claim is that it doesn't shout at you; a celebratory micro-interaction
contradicts the sentence sitting six inches to the left.

---

## 7. Reduced motion

```css
@media (prefers-reduced-motion: reduce)
```

Every transition/animation across bubbles, typing dots, day badges, calendar cards, memory rows,
and the strikethrough resolves to final state immediately — no spring, no loop, no glow
animation (the glow classes still apply, they just render with `animation: none`, i.e.
invisibly). Day-group swaps still happen (the old group is removed synchronously, no leave
transition) — the reduced-motion end state is legibly just the *last* day's exchange, not a full
multi-day transcript, since only one day-group is ever kept in the DOM regardless of motion
preference. That's consistent with the section's whole design, not a compromise specific to
reduced motion.

---

## 8. Accessibility

- Thread wrapped in `role="log"`, `aria-label="Example conversation"`, `aria-live="off"` (it
  loops — a screen reader re-announcing four days' worth of messages every ~20 seconds is
  hostile).
- Visually-hidden speaker prefix on each bubble: `<span class="sr-only">You: </span>` /
  `<span class="sr-only">Assistant: </span>`.
- Each day badge carries a visually-hidden full date alongside its visible short form.
- Calendar cards: `role="img"` with a complete descriptive `aria-label`, not a component
  screen readers have to piece together themselves.
- Bubble text must clear 4.5:1 — stock `--primary`/`--primary-foreground` passes comfortably.
- `Get started` needs a visible focus ring (shadcn's `--ring`).
- Zero-accent-color is the site-wide rule (locked in during design review, applies beyond just
  this section) — the only two exceptions anywhere are the Google Calendar icon's own real brand
  colors and the retrieved-state red, both real semantic/brand signals, never decorative.

---

## 9. Responsive

Below `860px`:

- Columns stack: headline block (including the agent memory panel, still directly below the
  CTA) first, thread second.
- The hero's fixed-height constraint (§1) is dropped on stacked layouts (`height: auto`) — it
  exists to protect vertical centering in a two-column desktop layout; a stacked mobile layout
  scrolls normally and doesn't need it.
- Bubbles to `82%` max-width (unchanged from desktop — already tuned tight).
- Headline drops to `clamp()`-scaled size.
- **Timeline unchanged.** Do not speed it up on mobile.

---

## 10. Explicitly not this

- No gradients, glows-as-decoration, glassmorphism, or backdrop blur. (The "just landed" pulse in
  §4.1 is a temporary semantic emphasis cue, not a decorative glow — it fires once, ties to a
  real event, and fades to nothing.)
- No orbiting nodes, network lines, or pulsing rings as ambient decoration.
- No parallax. The exchange is the motion; a second system will fight it.
- No sound.
- No decorative accent color anywhere. Two real exceptions only, both covered above: the Google
  Calendar icon's own brand colors, and the retrieved-state red. Nothing else on the page earns
  its place through hue — spacing and type do that work.
- No shadcn `Card` for bubbles or the calendar card — `Card`'s padding/shadow defaults fight the
  specific treatments in §3/§4.
- No checkmark/confetti/bounce on either payoff (§6.5).

---

## 11. Deliverables

```
src/components/hero.tsx              — section layout, headline, CTA, agent-memory panel
src/components/hero-thread.tsx       — day-group state machine, bubbles, typing indicator
src/components/hero-memory.tsx       — the agent memory panel, its own reveal/retrieve logic
src/components/calendar-card.tsx     — shared calendar card (booked/open variants), reused by
                                        hero-thread and (later) the dashboard
app/styleguide/page.tsx              — every token + these components at final state
```

`/styleguide` is the drift detector. Build it in the same pass, not later.
