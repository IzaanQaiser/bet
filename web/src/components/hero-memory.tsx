"use client";

export interface MemoryRowData {
  key: string;
  title: string;
  status: string;
  visible: boolean;
  glow: boolean;
  retrieved: boolean;
}

/**
 * hero-spec.md §1.2 — not framed as a UI the user opens. Dashed border
 * throughout (never solid) is deliberate: it's the "this is happening in
 * the background, agent-only" signal.
 *
 * Row reveal uses the CSS grid `0fr -> 1fr` technique (via inline style,
 * not a Tailwind utility — arbitrary `grid-template-rows` values don't
 * round-trip cleanly through the class-string/JIT pipeline) so the panel's
 * own height grows smoothly to an unknown content height, rather than
 * snapping in via display:none/block or guessing a max-height.
 */
export function HeroMemory({ rows }: { rows: MemoryRowData[] }) {
  return (
    <div className="mt-8 rounded-[10px] border-[1.5px] border-dashed border-border px-[18px] pb-[14px] pt-4">
      <p className="mb-[3px] flex items-center gap-[7px] font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
        <span className="h-[5px] w-[5px] rounded-full bg-muted-foreground animate-blip" />
        agent memory
      </p>
      <p className="mb-1 text-xs text-muted-foreground">remembered so you don&apos;t have to</p>

      {rows.map((row) => (
        <div
          key={row.key}
          className="grid transition-[grid-template-rows] ease-in-out"
          style={{
            gridTemplateRows: row.visible ? "1fr" : "0fr",
            transitionDuration: "480ms",
            transitionTimingFunction: "cubic-bezier(0.4, 0, 0.2, 1)",
          }}
        >
          <div className="overflow-hidden">
            <div
              className={`relative flex items-baseline justify-between gap-4 border-t border-dashed border-border py-2 font-mono text-xs ${
                row.glow ? "animate-glow-row rounded-md" : ""
              }`}
            >
              <span
                className="transition-colors ease-out"
                style={{
                  transitionDuration: "420ms",
                  color: row.retrieved ? "var(--muted-foreground)" : "color-mix(in oklch, var(--foreground) 78%, transparent)",
                }}
              >
                {row.title}
              </span>
              <span className="whitespace-nowrap text-muted-foreground">{row.status}</span>
              {/* the retrieved strikethrough spans the WHOLE row — title
                  through the status text — not just the title, so it reads
                  as "this whole entry is done," not half-crossed-off */}
              <span
                aria-hidden="true"
                className="absolute inset-x-0 top-1/2 h-[1.5px] origin-left bg-retrieved"
                style={{
                  transform: row.retrieved ? "scaleX(1)" : "scaleX(0)",
                  transitionProperty: "transform",
                  transitionDuration: "520ms",
                  transitionTimingFunction: "cubic-bezier(0.65, 0, 0.35, 1)",
                }}
              />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
