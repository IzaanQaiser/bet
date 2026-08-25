"use client";

import type { RefObject } from "react";
import { motion } from "framer-motion";
import { CalendarCard } from "@/components/calendar-card";
import type { Group, ThreadItem } from "@/lib/use-hero-sequence";

/** Purely presentational — all state/timing lives in useHeroSequence(). */
export function HeroThread({ groups, viewportRef }: { groups: Group[]; viewportRef: RefObject<HTMLDivElement | null> }) {
  return (
    <div
      ref={viewportRef}
      role="log"
      aria-label="Example conversation"
      aria-live="off"
      className="h-[min(640px,70vh)] w-full min-w-0 overflow-x-hidden overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      <div className="flex min-w-0 flex-col px-0.5 pb-2 pt-1">
        {groups.map((group) => (
          <motion.div
            key={group.id}
            initial={false}
            animate={
              group.leaving
                ? { y: -48, opacity: 0, transition: { duration: 0.42, ease: [0.4, 0, 1, 1] } }
                : { y: 0, opacity: 1 }
            }
            className="flex min-w-0 flex-col"
          >
            <div className="sticky top-0 z-[2] mb-1 flex justify-center bg-background px-0 pb-3.5 pt-2.5">
              <span className="rounded-full border border-border bg-muted px-3.5 py-1.5 font-mono text-[0.6875rem] tracking-[0.08em] text-muted-foreground">
                {group.label}
                <span className="sr-only"> — {group.srLabel}</span>
              </span>
            </div>

            <div className="flex min-w-0 flex-col">
              {group.items.map((item, i) => {
                // "last speaker" has to skip over calendar cards, not just look at
                // the immediately-preceding item — a card never changes who's
                // talking, matching the vanilla reference's persistent lastSpeaker
                // (only bubbles/typing update it, calendar cards never do)
                let prevDir: "in" | "out" | null = null;
                for (let j = i - 1; j >= 0; j--) {
                  const prevItem = group.items[j];
                  if (prevItem.kind === "bubble") { prevDir = prevItem.dir; break; }
                  if (prevItem.kind === "typing") { prevDir = "in"; break; }
                }
                const speakerChange =
                  (item.kind === "bubble" || item.kind === "typing") &&
                  prevDir !== null &&
                  prevDir !== (item.kind === "bubble" ? item.dir : "in");
                const spacing = i > 0 ? (speakerChange ? "mt-4" : "mt-[9px]") : "";

                if (item.kind === "bubble") {
                  return <Bubble key={item.id} item={item} spacing={spacing} />;
                }
                if (item.kind === "typing") {
                  return <TypingIndicator key={item.id} fading={item.fading} spacing={spacing} />;
                }
                return (
                  <div key={item.id} className={i > 0 ? "mt-[9px]" : ""}>
                    <CalendarCard
                      variant={item.variant}
                      activeDay={item.activeDay}
                      title={item.title}
                      time={item.time}
                      tag={item.tag}
                      glow={item.glow}
                    />
                  </div>
                );
              })}
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}

export function Bubble({ item, spacing }: { item: Extract<ThreadItem, { kind: "bubble" }>; spacing: string }) {
  return (
    <div className={`flex min-w-0 flex-col ${item.dir === "out" ? "items-end" : "items-start"} ${spacing}`}>
      <motion.div
        initial={{ opacity: 0, scale: 0.62, y: 14 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ opacity: { duration: 0.12 }, default: { type: "spring", stiffness: 500, damping: 32, mass: 0.9 } }}
        style={{ transformOrigin: item.dir === "out" ? "bottom right" : "bottom left" }}
        className={`max-w-[82%] min-w-0 break-words rounded-[18px] px-[13px] py-[9px] text-sm leading-snug ${
          item.dir === "out" ? "rounded-br-[4px] bg-primary text-primary-foreground" : "rounded-bl-[4px] bg-muted text-foreground"
        }`}
      >
        <span className="sr-only">{item.dir === "out" ? "You: " : "Assistant: "}</span>
        {item.text}
      </motion.div>
      {item.stamp && (
        <span
          className="mt-1 font-mono text-[0.6875rem] text-muted-foreground transition-opacity"
          style={{ opacity: item.showStamp ? 1 : 0, transitionDuration: "200ms" }}
        >
          {item.stamp}
        </span>
      )}
    </div>
  );
}

export function TypingIndicator({ fading, spacing }: { fading: boolean; spacing: string }) {
  return (
    <div className={`flex flex-col items-start ${spacing}`}>
      <motion.div
        initial={{ opacity: 0, scale: 0.62, y: 14 }}
        animate={{ opacity: fading ? 0 : 1, scale: 1, y: 0 }}
        transition={
          fading
            ? { duration: 0.14 }
            : { opacity: { duration: 0.12 }, default: { type: "spring", stiffness: 500, damping: 32, mass: 0.9 } }
        }
        style={{ transformOrigin: "bottom left" }}
        className="flex w-fit items-center gap-[5px] rounded-[18px] rounded-bl-[4px] bg-muted px-[15px] py-3"
      >
        {[0, 1, 2].map((dot) => (
          <span
            key={dot}
            className="h-1.5 w-1.5 animate-typing-dot rounded-full bg-muted-foreground"
            style={{ animationDelay: `${dot * 160}ms` }}
          />
        ))}
      </motion.div>
    </div>
  );
}
