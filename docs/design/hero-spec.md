# Hero Section — Implementation Spec

The landing page hero. One orchestrated animation carries the entire product
argument: a text message becomes a scheduled obligation, and then — days later,
unprompted — the system speaks first.

**Build as real DOM.** Not a video, webp, or Lottie. The copy in these bubbles
*is* the pitch; it needs to be editable, selectable, and crisp at any width.
Target under 8KB excluding fonts.

**Stack:** Next.js App Router, Tailwind, shadcn/ui (neutral base), Framer Motion.

---

## 1. Layout

```
┌───────────────────────────────┬───────────────────────────────┐
│                               │                               │
│  A CALMER INBOX               │              ┌──────────────┐ │
│                               │              │ user message │ │
│  Nothing to remember.         │              └──────────────┘ │
│  Just text it.                │                               │
│                               │  ┌────────────────┐           │
│  It listens, confirms once,   │  │ agent reply    │           │
│  and gets out of your way.    │  └────────────────┘           │
│                               │  4:41 PM                      │
│  Get started →                │                               │
│                               │              ┌─────┐          │
│                               │              │ yes │          │
│                               │              └─────┘          │
│                               │                               │
│                               │  ─────  FRI 28 AUG  ─────     │
│                               │                               │
│                               │  ┌────────────────┐           │
│                               │  │ reminder       │           │
│                               │  └────────────────┘           │
└───────────────────────────────┴───────────────────────────────┘
├───────────────────────────────────────────────────────────────┤
│  Buy birthday gift for Sarah                Fri 28 Aug, 12:00 PM │
│  Renew passport                                    Tue 1 Sept   │
│  Learn pottery                                        someday   │
└───────────────────────────────────────────────────────────────┘
```

- Two columns, `1fr 1fr`, divided by a `--border` hairline. No card, no shadow,
  no rounded container around either column.
- Ledger list sits below both, full width, separated by a `--border` hairline.
- Max width `1200px`, centered. Generous vertical padding — this section should
  own the fold and nothing should peek below it.

### 1.1 Copy

| Element | Text |
|---|---|
| Eyebrow | `A CALMER INBOX` |
| Headline | `Nothing to remember. Just text it.` |
| Subhead | `It listens, confirms once, and gets out of your way. No app to open.` |
| CTA | `Get started` |

CTA is a text link with a `--foreground` underline offset 6px, not a filled
button. A filled button competes with the dark sent-bubbles for the eye, and
there's only one action on this page anyway.

### 1.2 Ledger rows

| Title | Right column |
|---|---|
| Buy birthday gift for Sarah | `Fri 28 Aug, 12:00 PM` |
| Renew passport | `Tue 1 Sept` |
| Learn pottery | `someday` |

Title in `--foreground` medium weight. Right column in `--muted-foreground`,
mono/utility face, `tabular-nums`. `--border` hairline under each row.

`someday` is deliberate — it's the latent item with no deadline, and it's the
only hint on the page that the system holds things it isn't scheduling yet.
Leave it in. Don't explain it.

---

## 2. Type

Stock shadcn everything, with **one** deviation.

| Role | Face |
|---|---|
| Headline (`h1` only) | Instrument Serif, regular, `-0.02em`, `1.05` line-height |
| Everything else | Geist Sans |
| Timestamps, ledger dates, divider | Geist Mono |

The serif is the single non-default choice on the page. Everything around it
stays disciplined so it reads as intentional rather than decorative. Do not
extend the serif to `h2`, the eyebrow, or the CTA.

Eyebrow: Geist Mono, `0.75rem`, `0.12em` letter-spacing, uppercase,
`--muted-foreground`.

---

## 3. Bubbles

Standard messaging conventions. Getting these backwards reads as uncanny to
anyone who owns a phone.

| | User (sent) | Agent (received) |
|---|---|---|
| Alignment | right | left |
| Background | `--primary` | `--muted` |
| Text | `--primary-foreground` | `--foreground` |
| `transform-origin` | `bottom right` | `bottom left` |
| Max width | 78% of column | 78% of column |
| Padding | `10px 14px` | `10px 14px` |
| Radius | `18px`, bottom-right `4px` | `18px`, bottom-left `4px` |

The asymmetric corner is what makes a rounded rectangle read as a speech bubble.
Cheaper and cleaner than drawing an SVG tail.

Gap between consecutive bubbles: `10px`. Between speaker changes: `18px`.

**Never a raw hex, px value, or font-family in component code** — everything
resolves through Tailwind classes backed by the shadcn CSS variables. If a value
you need doesn't exist as a token, stop and add it to `globals.css` first.

### 3.1 Timestamp

One only, under the agent's first reply. `4:41 PM`, Geist Mono, `0.6875rem`,
`--muted-foreground`. Fades in 200ms after its bubble settles.

One timestamp is a detail. Three is clutter.

### 3.2 Date divider

Centered, full-column width:

```
────────────  FRI 28 AUG  ────────────
```

Geist Mono, `0.6875rem`, `0.1em` letter-spacing, uppercase,
`--muted-foreground`. Flanking rules are `--border` hairlines,
`flex-1`, vertically centered.

This is the native messaging idiom for a gap in time. It requires zero
explanation because every phone already does it.

---

## 4. Motion

### 4.1 Bubble entrance

iMessage bubbles don't slide or fade — they **pop**, with overshoot, anchored to
the corner they came from.

```
from:  opacity 0, scale 0.62, translateY 14px
to:    opacity 1, scale 1,    translateY 0
```

- **Framer Motion:** `{ type: "spring", stiffness: 500, damping: 32, mass: 0.9 }`
- **CSS fallback:** `380ms cubic-bezier(0.34, 1.56, 0.64, 1)`

Opacity resolves faster than transform — ramp it over the first ~120ms so the
bubble is fully visible while still springing. A bubble that fades across its
whole spring looks sluggish.

Transform and opacity only. Never animate `width`, `height`, `top`, or `left`,
or it will jank on a mid-range laptop during a demo.

### 4.2 Typing indicator

The pause is the only place the agent appears to *think*. Don't skip it.

Bubble enters with the received-bubble spring. Inside, three dots:

- `6px` diameter, `--muted-foreground`, `5px` gap
- Each: `scale 0.75 → 1.0`, `opacity 0.4 → 1.0`
- `1200ms` loop, `ease-in-out`, **160ms stagger** between dots

On exit, don't fade the typing bubble out and the real bubble in separately —
that reads as two events. Cross-fade over `140ms` while the real bubble springs
in from the same origin, so the typing bubble appears to *become* the message.

**The reminder in Beat 2 gets no typing indicator.** A typing indicator means
"I'm responding to you." A reminder is not a response — nobody asked. That
absence is the whole point of the second beat; adding dots there would quietly
undo it.

### 4.3 Thread reflow

When a new bubble enters, everything above it translates up by the new bubble's
height. Same spring, no stagger. This is what sells a live conversation rather
than a list being revealed.

### 4.4 Timeline

Trigger once on intersection at 40% visibility. Not on page load — wait until
someone is actually looking at it.

| t (ms) | Event |
|---|---|
| — | **Beat 1 — capture** |
| 0 | User bubble enters (right): `buy a birthday gift for sarah, friday at noon` |
| 700 | Typing indicator enters (left) |
| 2000 | Typing cross-fades into agent bubble: `i'll add that for friday at noon — sound good?` |
| 2200 | Timestamp fades in |
| 3000 | User bubble enters (right): `yes` |
| 3700 | Ledger row `Buy birthday gift for Sarah` inserts into the list below |
| 3900 | Hairline wipes down the row's left edge, holds 600ms, fades |
| — | **Time passes** |
| 4900 | Date divider fades in: `FRI 28 AUG` |
| — | **Beat 2 — the system speaks first** |
| 5500 | Reminder bubble enters (left), no typing indicator |
| 5500–9500 | Hold |
| 9500 | Reset, replay |

The 700ms and 3000ms gaps are human reading pauses. Compress them and the whole
thing feels like an ad. Stretch past ~1.2s and people scroll away.

### 4.5 The two payoffs

Beat 1's payoff is `t=3700`: the ledger row inserting. The message became a
scheduled thing. Enter from `translateY(8px)` + `opacity 0` over `400ms`
`ease-out`, then a `2px --foreground` hairline wipes down the left edge and
fades to nothing.

Beat 2's payoff is `t=5500`: a message arrives with no user message before it.
Same spring, nothing special — the restraint *is* the effect. The divider
established that days went by; the bubble arriving alone establishes that
nobody asked for it.

Reminder copy:

> `Sarah's gift — today at noon. Your morning's clear if you want to handle it early.`

Naming the evidence (`your morning's clear`) is what makes this read as
intelligence rather than a calendar alert. Keep that clause.

No checkmark, no confetti, no scale-bounce, no color shift on either payoff. The
product's claim is that it doesn't shout at you; a celebratory micro-interaction
here contradicts the sentence sitting six inches to the left.

### 4.6 Loop

Hold the completed state 4s, then reset and replay — **only while in the
viewport.** Pause on exit. A loop running in a background tab is a battery
complaint.

Reset is instant, not animated. Fading everything out and back in draws
attention to the mechanism.

---

## 5. Reduced motion

```css
@media (prefers-reduced-motion: reduce)
```

Every bubble, the divider, and the ledger row render at final state
immediately. No typing indicator, no hairline wipe, no loop. The component still
communicates everything — it's a legible transcript.

Do not implement this as a slower version of the same animation.

---

## 6. Accessibility

- Thread wrapped in `role="log"`, `aria-label="Example conversation"`.
- Because it loops: `aria-live="off"`. A screen reader re-announcing four
  messages every ten seconds is hostile.
- Visually-hidden speaker prefix on each bubble: `<span class="sr-only">You:
  </span>` / `<span class="sr-only">Assistant: </span>`. Without it the
  transcript is four unattributed sentences.
- The date divider needs a visually-hidden `Friday 28 August` — the abbreviated
  uppercase form reads badly aloud.
- Bubble text must clear 4.5:1. Stock `--primary` / `--primary-foreground`
  passes comfortably; re-check if the palette is ever changed.
- `Get started` needs a visible focus ring. Use shadcn's `--ring`.

---

## 7. Responsive

Below `768px`:

- Columns stack. Headline block first, thread below, ledger last.
- Bubbles to `86%` max-width.
- Headline drops to `clamp()`-scaled size; the serif needs a tighter
  line-height at small sizes, not looser.
- **Timeline unchanged.** Do not speed it up on mobile.

The thread container has a `min-height` equal to its final rendered height,
reserved before the animation starts. Otherwise the ledger row entering at
`t=3700` shifts the page under someone's thumb.

---

## 8. Explicitly not this

- No gradients, glows, glassmorphism, or backdrop blur.
- No orbiting nodes, network lines, or pulsing rings.
- No parallax. The exchange is the motion; a second system will fight it.
- No sound.
- No accent color. The palette is shadcn neutral — near-black, greys, white.
  Every element on this page earns its place through spacing and type, not hue.
- No shadcn `Card` for the bubbles. Card's padding and shadow defaults will
  fight §3.

---

## 9. Deliverables

```
src/components/hero.tsx           — section layout, headline, CTA
src/components/hero-thread.tsx    — animated chat, owns the timeline
src/components/hero-ledger.tsx    — the three rows
app/styleguide/page.tsx           — every token + this component at final state
```

`/styleguide` is the drift detector. Build it in the same pass, not later.