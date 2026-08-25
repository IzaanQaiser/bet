import { Bubble, TypingIndicator } from "@/components/hero-thread";
import { CalendarCard } from "@/components/calendar-card";
import { HeroMemory } from "@/components/hero-memory";
import type { MemoryRowData } from "@/components/hero-memory";

/**
 * hero-spec.md §11 — the drift detector. Every token and hero component
 * rendered at rest, reusing the real production components (not redrawn
 * copies) so this page breaks the moment the real ones drift from spec.
 */
export const metadata = {
  title: "Styleguide",
};

const COLOR_TOKENS: { name: string; var: string; note?: string }[] = [
  { name: "background", var: "--background" },
  { name: "foreground", var: "--foreground" },
  { name: "muted", var: "--muted" },
  { name: "muted-foreground", var: "--muted-foreground" },
  { name: "border", var: "--border" },
  { name: "primary", var: "--primary" },
  { name: "primary-foreground", var: "--primary-foreground" },
  { name: "retrieved", var: "--retrieved", note: "exception — semantic \"done\" signal, §1.2/§8" },
];

const SAMPLE_ROWS: MemoryRowData[] = [
  { key: "a", title: "incident-triage agent crew", status: "someday", visible: true, glow: false, retrieved: false },
  { key: "b", title: "buy birthday gift, sarah", status: "fri", visible: true, glow: true, retrieved: false },
  { key: "c", title: "book dentist appointment", status: "mon", visible: true, glow: false, retrieved: true },
];

function Section({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-border py-10 first:border-t-0 first:pt-0">
      <div className="mb-6 flex items-baseline justify-between gap-4">
        <h2 className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">{title}</h2>
        {note && <p className="font-mono text-[0.6875rem] text-muted-foreground">{note}</p>}
      </div>
      {children}
    </section>
  );
}

export default function StyleguidePage() {
  return (
    <div className="mx-auto max-w-[900px] px-8 py-16">
      <p className="mb-[18px] font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">bet</p>
      <h1 className="mb-3 font-serif text-[clamp(30px,4.4vw,46px)] leading-[1.05] tracking-[-0.02em]">
        Styleguide
      </h1>
      <p className="mb-16 max-w-[52ch] text-base leading-relaxed text-muted-foreground">
        Every token and hero component at final state, per hero-spec.md §11. Built in the same
        pass as the hero so drift shows up here first.
      </p>

      <Section title="Color">
        <div className="grid grid-cols-2 gap-x-8 gap-y-5 sm:grid-cols-4">
          {COLOR_TOKENS.map((t) => (
            <div key={t.var}>
              <div
                className="mb-2 h-14 w-full rounded-[10px] border border-border"
                style={{ background: `var(${t.var})` }}
              />
              <p className="font-mono text-xs text-foreground">{t.name}</p>
              {t.note && <p className="mt-0.5 font-mono text-[0.6875rem] text-muted-foreground">{t.note}</p>}
            </div>
          ))}
        </div>
      </Section>

      <Section title="Type" note="one serif moment per page — this h1 is it">
        <div className="flex flex-col gap-5">
          <div>
            <p className="mb-1 font-mono text-[0.6875rem] text-muted-foreground">Instrument Serif — h1 only</p>
            <p className="font-serif text-[32px] leading-[1.05] tracking-[-0.02em]">Nothing to remember.</p>
          </div>
          <div>
            <p className="mb-1 font-mono text-[0.6875rem] text-muted-foreground">Geist Sans — everything else</p>
            <p className="text-base leading-relaxed text-foreground">
              It listens and quietly keeps track of everything else.
            </p>
          </div>
          <div>
            <p className="mb-1 font-mono text-[0.6875rem] text-muted-foreground">
              Geist Mono — timestamps, day badges, calendar/memory labels
            </p>
            <p className="font-mono text-[0.6875rem] tracking-[0.08em] text-muted-foreground">4:41 PM</p>
          </div>
        </div>
      </Section>

      <Section title="Bubbles" note="82% max-width, capped at a fixed-width column — never grows the layout">
        <div className="mx-auto flex w-[420px] max-w-full flex-col gap-[9px] rounded-[10px] border border-border p-4">
          <Bubble
            item={{ kind: "bubble", id: "sg1", dir: "out", text: "yo i need to get sarah a gift for her birthday", showStamp: false }}
            spacing=""
          />
          <Bubble
            item={{
              kind: "bubble",
              id: "sg2",
              dir: "in",
              text: "bet, i'll set a reminder for friday at noon",
              stamp: "4:41 PM",
              showStamp: true,
            }}
            spacing="mt-4"
          />
          <Bubble
            item={{
              kind: "bubble",
              id: "sg3",
              dir: "out",
              text: "yo i wanna build an agent crew for the hackathon that triages prod incidents and drafts the postmortem",
              showStamp: false,
            }}
            spacing="mt-4"
          />
          <TypingIndicator fading={false} spacing="mt-4" />
        </div>
      </Section>

      <Section title="Calendar card">
        <div className="flex flex-wrap gap-6">
          <CalendarCard variant="booked" activeDay={5} title="Buy birthday gift for Sarah" time="12:00 PM" tag="Added to Google Calendar" glow />
          <CalendarCard variant="open" activeDay={4} title="Incident-triage agent" time="2:00 – 4:00 PM" tag="Free on your Google Calendar" />
        </div>
      </Section>

      <Section title="Agent memory panel" note="dashed border throughout — the signal that it's happening in the background">
        <div className="max-w-[420px]">
          <HeroMemory rows={SAMPLE_ROWS} />
        </div>
      </Section>

      <Section title="Day badge">
        <span className="rounded-full border border-border bg-muted px-3.5 py-1.5 font-mono text-[0.6875rem] tracking-[0.08em] text-muted-foreground">
          Wednesday September 9th
        </span>
      </Section>
    </div>
  );
}
