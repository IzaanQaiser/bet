"use client";

interface MemoryListItem {
  id: string;
  title: string;
  state?: string;
  due_at?: string | null;
  updated_at?: string;
  last_message_at?: string | null;
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
    <div className="group flex items-center justify-between gap-4 py-3">
      <span className="text-[15px] font-medium leading-snug">{title}</span>
      <span className="flex shrink-0 items-center gap-3">
        <span className="whitespace-nowrap font-mono text-xs tabular-nums text-muted-foreground">
          {meta}
        </span>
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Remove ${title}`}
          className="text-sm text-muted-foreground/50 hover:text-destructive"
        >
          ×
        </button>
      </span>
    </div>
  );
}

export function MemoryList({ inProgress, committed, timeZone, onDelete }: MemoryListProps) {
  if (inProgress.length === 0 && committed.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        Nothing yet — text bet something and it&apos;ll show up here.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      {inProgress.length > 0 && (
        <div>
          <p className="mb-1 font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
            In progress
          </p>
          <div className="divide-y divide-border">
            {inProgress.map((row) => {
              const rel = relativeTime(row.last_message_at ?? row.updated_at);
              const state = humanizeState(row.state);
              return (
                <Row
                  key={row.id}
                  title={row.title}
                  meta={rel ? `${state} · ${rel}` : state}
                  onDelete={() => onDelete(row.id)}
                />
              );
            })}
          </div>
        </div>
      )}

      {committed.length > 0 && (
        <div>
          <p className="mb-1 font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
            Committed
          </p>
          <div className="divide-y divide-border">
            {committed.map((row) => (
              <Row
                key={row.id}
                title={row.title}
                meta={formatCommittedTime(row.due_at, timeZone)}
                onDelete={() => onDelete(row.id)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
