"use client";

import Link from "next/link";
import { useHeroSequence } from "@/lib/use-hero-sequence";
import { HeroThread } from "@/components/hero-thread";
import { HeroMemory } from "@/components/hero-memory";

/**
 * hero-spec.md §1 — two columns, `1fr 1.1fr`, both pinned to a fixed
 * height so neither the growing memory panel nor the scrolling thread can
 * shift the page's vertical centering. Left column is one aligned stack:
 * eyebrow, h1, lead, CTA, then the agent memory panel directly below it —
 * not a separate floating element.
 */
export function Hero() {
  const { groups, memoryRows, viewportRef } = useHeroSequence();

  return (
    <div className="mx-auto flex min-h-screen max-w-[1200px] flex-col justify-center px-8 py-10">
      <div className="grid h-[min(640px,70vh)] grid-cols-1 items-start gap-8 md:grid-cols-[1fr_1.1fr] md:gap-14 md:h-[min(640px,70vh)]">
        <div className="flex flex-col">
          <p className="mb-[18px] font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">bet</p>
          <h1 className="mb-[18px] font-serif text-[clamp(30px,4.4vw,46px)] leading-[1.05] tracking-[-0.02em] text-balance">
            Nothing to remember.
            <br />
            Just text it.
          </h1>
          <p className="mb-6 max-w-[34ch] text-base leading-relaxed text-muted-foreground">
            It listens and quietly keeps track of everything else, including when you&apos;re actually free enough
            to do it.
          </p>
          {/* plan Phase 2 builds /waitlist — dead link until then, expected */}
          <Link
            href="/waitlist"
            className="self-start text-[15px] font-medium text-foreground underline decoration-foreground/40 underline-offset-[6px] hover:decoration-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-foreground"
          >
            Get started
          </Link>

          <HeroMemory rows={memoryRows} />
        </div>

        <HeroThread groups={groups} viewportRef={viewportRef} />
      </div>
    </div>
  );
}
