"use client";

interface MemoryListItem {
  id: string;
  title: string;
  state?: string;
  due_at?: string | null;
  updated_at?: string;
  last_message_at?: string | null;
  // "obligation" | "latent" — only ever present on a committed row.
  type?: string;
}

interface MemoryListProps {
  inProgress: MemoryListItem[];
  committed: MemoryListItem[];
  timeZone: string;
  onDelete: (id: string) => void;
}

function humanizeState(state: string | undefined): string {
  if (!state) return "";
  return state.toLowerCase().replace(/_/g, " ");
}

// Weekday + day + time, not just weekday — two items due on the same day
// at different times used to render as literal duplicates ("Tue" / "Tue").
function formatCommittedTime(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return "committed";
  const d = new Date(iso);
  const weekday = d.toLocaleDateString("en-US", { timeZone, weekday: "short" });
  const day = d.toLocaleDateString("en-US", { timeZone, day: "numeric" });
  const time = d.toLocaleTimeString("en-US", { timeZone, hour: "numeric", minute: "2-digit" });
  return `${weekday} ${day}, ${time}`;
}

function relativeTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const minutes = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// Agent-memory row styling (hero-spec.md §1.2, originally the landing
// hero's own small widget — always meant to extend to the dashboard per
// that same doc, not a one-off reskin here): dashed top rule between
// rows, not solid, matching the panel's own dashed border language.
function Row({
  title,
  meta,
  onDelete,
}: {
  title: string;
  meta: string;
  onDelete: () => void;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-t border-dashed border-border py-2 font-mono text-xs">
      <span className="text-[color-mix(in_oklch,var(--foreground)_78%,transparent)]">
        {title}
      </span>
      <span className="flex shrink-0 items-baseline gap-1.5">
        <span className="whitespace-nowrap text-muted-foreground">{meta}</span>
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Remove ${title}`}
          className="text-muted-foreground/60 hover:text-destructive"
        >
          ×
        </button>
      </span>
    </div>
  );
}

function Group({
  label,
  rows,
  meta,
  onDelete,
}: {
  label: string;
  rows: MemoryListItem[];
  meta: (row: MemoryListItem) => string;
  onDelete: (id: string) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <div>
      <p className="mb-0.5 font-mono text-[11px] uppercase tracking-[0.1em] text-muted-foreground/70">
        {label}
      </p>
      <div>
        {rows.map((row) => (
          <Row key={row.id} title={row.title} meta={meta(row)} onDelete={() => onDelete(row.id)} />
        ))}
      </div>
    </div>
  );
}

export function MemoryList({ inProgress, committed, timeZone, onDelete }: MemoryListProps) {
  // Separated, not inferred client-side from a null due_at — an
  // email-action obligation can legitimately have one too, so type is
  // the only reliable signal for "this is a someday idea, not a real
  // scheduled thing" (dashboard-svc's own note on why it sends type).
  const scheduled = committed.filter((row) => row.type !== "latent");
  const ideas = committed.filter((row) => row.type === "latent");
  const isEmpty = inProgress.length === 0 && committed.length === 0;

  return (
    <div className="rounded-[10px] border-[1.5px] border-dashed border-border px-[18px] pb-[14px] pt-4">
      <p className="mb-[3px] flex items-center gap-[7px] font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
        <span className="h-[5px] w-[5px] rounded-full bg-muted-foreground animate-blip" />
        agent memory
      </p>
      <p className="mb-2 text-xs text-muted-foreground">remembered so you don&apos;t have to</p>

      {isEmpty ? (
        <p className="border-t border-dashed border-border py-2 text-xs text-muted-foreground">
          Nothing yet — text bet something and it&apos;ll show up here.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <Group
            label="In progress"
            rows={inProgress}
            meta={(row) => {
              const rel = relativeTime(row.last_message_at ?? row.updated_at);
              const state = humanizeState(row.state);
              return rel ? `${state} · ${rel}` : state;
            }}
            onDelete={onDelete}
          />
          <Group
            label="Committed"
            rows={scheduled}
            meta={(row) => formatCommittedTime(row.due_at, timeZone)}
            onDelete={onDelete}
          />
          <Group label="Ideas" rows={ideas} meta={() => "someday"} onDelete={onDelete} />
        </div>
      )}
    </div>
  );
}
