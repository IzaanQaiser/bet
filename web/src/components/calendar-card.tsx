"use client";

import { motion } from "framer-motion";

const DAYS = ["S", "M", "T", "W", "T", "F", "S"] as const;

// A real, recognizable rendition of the Google Calendar icon — the
// four-color corner treatment plus a blue date number — not a generic
// calendar glyph. hero-spec.md §4: this is what makes "it actually writes
// to your calendar" legible instead of just claimed in a text bubble. The
// icon's own brand colors are a deliberate exception to the zero-accent
// rule (§8) — a real third-party mark, not a decorative accent.
function GoogleCalendarIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 36 36" aria-hidden="true">
      <path d="M26 4h4a2 2 0 0 1 2 2v4h-6V4z" fill="#4285F4" />
      <path d="M32 10v4h-6v-4h6z" fill="#4285F4" />
      <path d="M4 10V6a2 2 0 0 1 2-2h4v6H4z" fill="#EA4335" />
      <path d="M10 4h6v6h-6V4z" fill="#EA4335" />
      <path d="M4 10h6v6H4v-6z" fill="#FBBC04" />
      <path d="M4 16h6v6H4v-6z" fill="#FBBC04" />
      <path d="M4 22v6a2 2 0 0 0 2 2h4v-8H4z" fill="#34A853" />
      <path d="M10 30h6v-8h-6v8z" fill="#34A853" />
      <path d="M26 30h4a2 2 0 0 0 2-2v-4h-6v6z" fill="#4285F4" />
      <path d="M26 22h6v-6h-6v6z" fill="#4285F4" />
      <rect x="10" y="10" width="16" height="16" fill="#fff" />
      <text
        x="18"
        y="22.5"
        fontSize="12"
        fontWeight="700"
        textAnchor="middle"
        fill="#1A73E8"
        fontFamily="Arial, sans-serif"
      >
        31
      </text>
    </svg>
  );
}

export type CalendarCardVariant = "booked" | "open";

export interface CalendarCardProps {
  variant: CalendarCardVariant;
  /** 0 = Sunday .. 6 = Saturday */
  activeDay: number;
  title: string;
  time: string;
  tag: string;
  glow?: boolean;
  // Optional, dashboard-only — the landing hero never passes this, so
  // nothing renders differently there. A sibling of the role="img" card,
  // not a child of it, so the card's accessible name stays exactly the
  // single descriptive label it already is.
  onDelete?: () => void;
}

export function CalendarCard({
  variant,
  activeDay,
  title,
  time,
  tag,
  glow,
  onDelete,
}: CalendarCardProps) {
  const isOpen = variant === "open";
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.42, ease: "easeOut" }}
      className="relative self-start max-w-[82%]"
    >
      {onDelete && (
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Remove ${title}`}
          className="absolute -right-2 -top-2 z-[1] flex h-5 w-5 items-center justify-center rounded-full border border-border bg-background text-xs text-muted-foreground hover:text-destructive"
        >
          ×
        </button>
      )}
      <div
        role="img"
        aria-label={`Google Calendar — ${title}, ${time}${isOpen ? " — free" : ""}`}
        className={`inline-flex min-w-[240px] flex-col overflow-hidden rounded-[10px] border border-border bg-background ${
          glow ? "animate-glow-ring" : ""
        }`}
      >
        <div className="flex items-center gap-[7px] border-b border-border bg-muted/50 px-3 py-2">
          <GoogleCalendarIcon />
          <span className="font-mono text-[10px] font-medium tracking-[0.03em] text-muted-foreground">
            {tag}
          </span>
        </div>
        <div className="flex border-b border-border">
          {DAYS.map((d, i) => (
            <span
              key={i}
              className={`flex-1 py-1.5 text-center font-mono text-[10px] ${
                i === activeDay
                  ? "font-semibold text-foreground shadow-[inset_0_-2px_0_var(--foreground)]"
                  : "text-muted-foreground"
              }`}
            >
              {d}
            </span>
          ))}
        </div>
        <div className="flex items-start gap-2.5 px-[13px] py-[11px]">
          <span
            className={`self-stretch w-[3px] shrink-0 rounded-sm ${
              isOpen ? "w-0 border-[1.5px] border-dashed border-muted-foreground" : "bg-foreground"
            }`}
          />
          <div>
            <p className={`m-0 mb-0.5 text-[13px] font-medium ${isOpen ? "italic text-muted-foreground" : ""}`}>
              {title}
            </p>
            <p className="m-0 font-mono text-[11px] text-muted-foreground">{time}</p>
          </div>
        </div>
      </div>
    </motion.div>
  );
}
