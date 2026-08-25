"use client";

import { useEffect, useRef, useState } from "react";
import { sleep } from "@/lib/sleep";
import type { CalendarCardVariant } from "@/components/calendar-card";
import type { MemoryRowData } from "@/components/hero-memory";

export type ThreadItem =
  | { kind: "bubble"; id: string; dir: "in" | "out"; text: string; stamp?: string; showStamp: boolean }
  | { kind: "typing"; id: string; fading: boolean }
  | {
      kind: "calendar";
      id: string;
      variant: CalendarCardVariant;
      activeDay: number;
      title: string;
      time: string;
      tag: string;
      glow: boolean;
    };

export interface Group {
  id: string;
  label: string;
  srLabel: string;
  items: ThreadItem[];
  leaving: boolean;
}

const INITIAL_MEMORY: MemoryRowData[] = [
  { key: "hackathon", title: "incident-triage agent crew", status: "someday", visible: false, glow: false, retrieved: false },
  { key: "gift", title: "buy birthday gift, sarah", status: "fri", visible: false, glow: false, retrieved: false },
];

let uid = 0;
function nextId() {
  uid += 1;
  return `t${uid}`;
}

/**
 * hero-spec.md §5 — the day-switching state machine, plus the full
 * 4-beat timeline (§6.3) and the agent-memory panel's state (§1.2),
 * all driven from one place since the two views (thread + memory) need
 * to stay perfectly in sync (the glow-sync in §4.1 in particular).
 *
 * Only ONE group is ever "current" at a time; when a new day starts, the
 * previous one plays a leave transition and is dropped from state
 * entirely, not just scrolled past. Two earlier scroll-position-only
 * approaches both failed because a single day's own content can be
 * shorter than the fixed viewport — there's nothing to scroll past
 * regardless of the computed target. Removing it from state is what
 * actually guarantees "only one day visible."
 */
export function useHeroSequence() {
  const [groups, setGroups] = useState<Group[]>([]);
  const [memoryRows, setMemoryRows] = useState<MemoryRowData[]>(INITIAL_MEMORY);
  const viewportRef = useRef<HTMLDivElement>(null);
  const cancelledRef = useRef(false);
  const runningRef = useRef(false);
  const reducedRef = useRef(false);

  useEffect(() => {
    reducedRef.current = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }, []);

  function scrollDown() {
    const el = viewportRef.current;
    if (!el) return;
    const target = el.scrollHeight - el.clientHeight;
    if (reducedRef.current) el.scrollTop = target;
    else el.scrollTo({ top: target, behavior: "smooth" });
  }

  function addBubble(dir: "in" | "out", text: string, stamp?: string) {
    const id = nextId();
    setGroups((prev) =>
      prev.map((g) =>
        g.leaving ? g : { ...g, items: [...g.items, { kind: "bubble", id, dir, text, stamp, showStamp: false }] },
      ),
    );
    requestAnimationFrame(scrollDown);
    if (stamp) {
      setTimeout(
        () => {
          setGroups((prev) =>
            prev.map((g) => ({
              ...g,
              items: g.items.map((it) => (it.kind === "bubble" && it.id === id ? { ...it, showStamp: true } : it)),
            })),
          );
        },
        reducedRef.current ? 0 : 200,
      );
    }
    return id;
  }

  function addTyping() {
    const id = nextId();
    setGroups((prev) =>
      prev.map((g) => (g.leaving ? g : { ...g, items: [...g.items, { kind: "typing", id, fading: false }] })),
    );
    requestAnimationFrame(scrollDown);
    return id;
  }

  async function replaceTypingWithBubble(typingId: string, text: string, stamp?: string) {
    setGroups((prev) =>
      prev.map((g) => ({
        ...g,
        items: g.items.map((it) => (it.kind === "typing" && it.id === typingId ? { ...it, fading: true } : it)),
      })),
    );
    await sleep(140, reducedRef.current);
    setGroups((prev) => prev.map((g) => ({ ...g, items: g.items.filter((it) => it.id !== typingId) })));
    return addBubble("in", text, stamp);
  }

  function showCalendarCard(opts: {
    variant: CalendarCardVariant;
    activeDay: number;
    title: string;
    time: string;
    tag: string;
  }) {
    const id = nextId();
    setGroups((prev) =>
      prev.map((g) => (g.leaving ? g : { ...g, items: [...g.items, { kind: "calendar", id, glow: false, ...opts }] })),
    );
    requestAnimationFrame(scrollDown);
    return id;
  }

  function glowCalendarCard(id: string) {
    setGroups((prev) =>
      prev.map((g) => ({
        ...g,
        items: g.items.map((it) => (it.kind === "calendar" && it.id === id ? { ...it, glow: true } : it)),
      })),
    );
  }

  /** hero-spec.md §4.1 — fires on the exact same tick as the paired calendar card, so the
   *  two "just landed" cues read as one event, not two. */
  function insertMemoryRow(key: string, syncCardId?: string) {
    setMemoryRows((prev) => prev.map((r) => (r.key === key ? { ...r, visible: true } : r)));
    setTimeout(
      () => {
        setMemoryRows((prev) => prev.map((r) => (r.key === key ? { ...r, glow: true } : r)));
        if (syncCardId) glowCalendarCard(syncCardId);
      },
      reducedRef.current ? 0 : 450,
    );
  }

  function retrieveMemoryRow(key: string) {
    setMemoryRows((prev) => prev.map((r) => (r.key === key ? { ...r, retrieved: true } : r)));
  }

  async function showDayHeader(label: string, srLabel: string) {
    const hasCurrent = await new Promise<boolean>((resolve) => {
      setGroups((prev) => {
        resolve(prev.some((g) => !g.leaving));
        return prev.map((g) => (g.leaving ? g : { ...g, leaving: true }));
      });
    });
    if (hasCurrent) {
      await sleep(420, reducedRef.current);
      setGroups((prev) => prev.filter((g) => !g.leaving));
    }
    const id = nextId();
    setGroups((prev) => [...prev, { id, label, srLabel, items: [], leaving: false }]);
    requestAnimationFrame(scrollDown);
  }

  function reset() {
    setGroups([]);
    setMemoryRows(INITIAL_MEMORY);
    const el = viewportRef.current;
    if (el) el.scrollTop = 0;
  }

  async function playOnce() {
    const reduced = reducedRef.current;
    const cancelled = () => cancelledRef.current;

    // ---- beat 1: capture, proven against a real calendar ----
    await showDayHeader("Monday September 7th", "Monday, September 7th");
    await sleep(600, reduced); if (cancelled()) return;
    addBubble("out", "yo i need to get sarah a gift for her birthday");
    await sleep(1000, reduced); if (cancelled()) return;
    let t = addTyping();
    await sleep(1800, reduced); if (cancelled()) return;
    await replaceTypingWithBubble(t, "bet, i'll set a reminder for friday at noon", "4:41 PM");
    await sleep(1200, reduced); if (cancelled()) return;
    addBubble("out", "aight");
    await sleep(1100, reduced); if (cancelled()) return;
    const giftCard = showCalendarCard({
      variant: "booked", activeDay: 5, title: "Buy birthday gift for Sarah", time: "12:00 PM", tag: "Added to Google Calendar",
    });
    await sleep(700, reduced); if (cancelled()) return;
    insertMemoryRow("gift", giftCard);
    await sleep(2200, reduced); if (cancelled()) return;

    // ---- beat 2: a "someday" idea, captured casually — not scheduled, just held ----
    await showDayHeader("Wednesday September 9th", "Wednesday, September 9th");
    await sleep(1000, reduced); if (cancelled()) return;
    addBubble("out", "yo i wanna build an agent crew for the hackathon that triages prod incidents and drafts the postmortem");
    await sleep(1100, reduced); if (cancelled()) return;
    t = addTyping();
    await sleep(1800, reduced); if (cancelled()) return;
    await replaceTypingWithBubble(t, "bet, i'll keep that in mind");
    await sleep(1300, reduced); if (cancelled()) return;
    insertMemoryRow("hackathon");
    await sleep(2000, reduced); if (cancelled()) return;

    // ---- beat 3: speaks first — proactive, capacity-aware, no typing indicator ----
    await showDayHeader("Thursday September 10th", "Thursday, September 10th");
    await sleep(1200, reduced); if (cancelled()) return;
    addBubble("in", "yo this afternoon's wide open, wanna knock out the incident-triage agent?");
    await sleep(450, reduced); if (cancelled()) return;
    retrieveMemoryRow("hackathon");
    await sleep(450, reduced); if (cancelled()) return;
    showCalendarCard({
      variant: "open", activeDay: 4, title: "Incident-triage agent", time: "2:00 – 4:00 PM", tag: "Free on your Google Calendar",
    });
    await sleep(1500, reduced); if (cancelled()) return;
    addBubble("out", "bet, let's do it");
    await sleep(2400, reduced); if (cancelled()) return;

    // ---- beat 4: the day-of reminder, closing the loop back to beat 1 ----
    await showDayHeader("Friday September 11th", "Friday, September 11th");
    await sleep(1200, reduced); if (cancelled()) return;
    addBubble("in", "don't forget, sarah's gift today at noon");
    await sleep(450, reduced); if (cancelled()) return;
    retrieveMemoryRow("gift");
    await sleep(450, reduced); if (cancelled()) return;
    showCalendarCard({
      variant: "booked", activeDay: 5, title: "Buy birthday gift for Sarah", time: "12:00 PM", tag: "On your Google Calendar",
    });
    await sleep(1500, reduced); if (cancelled()) return;
    addBubble("out", "bet, appreciate it");
    await sleep(4200, reduced);
  }

  async function loop() {
    if (runningRef.current) return;
    runningRef.current = true;
    cancelledRef.current = false;
    while (!cancelledRef.current) {
      reset();
      await playOnce();
      if (cancelledRef.current) break;
    }
    runningRef.current = false;
  }

  function stop() {
    cancelledRef.current = true;
    runningRef.current = false;
  }

  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => entries.forEach((entry) => (entry.isIntersecting ? loop() : stop())),
      { threshold: 0.3 },
    );
    observer.observe(el);
    return () => {
      observer.disconnect();
      stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { groups, memoryRows, viewportRef };
}
