"use client";

import { useState } from "react";

export interface ProfileUpdate {
  timezone?: string;
  working_hours_start?: string;
  working_hours_end?: string;
}

interface SettingsPanelProps {
  timezone: string;
  workingHoursStart: string;
  workingHoursEnd: string;
  onSave: (update: ProfileUpdate) => Promise<boolean>;
}

// Every real IANA zone name, natively — no curated city list, no external
// geocoding API. Detection itself (below) reads the browser/OS's own
// configured zone directly, same Intl call already used in the
// registration flow (web-division plan Phase 4) — no location permission
// prompt, no GPS.
const TIMEZONES = Intl.supportedValuesOf("timeZone");
const DETECTED_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

// dashboard-svc's GET /me/profile returns a Postgres `time` column as a
// full "HH:MM:SS" ISO string; <input type="time"> wants "HH:MM".
function toInputTime(isoTime: string): string {
  return isoTime.slice(0, 5);
}

export function SettingsPanel({
  timezone,
  workingHoursStart,
  workingHoursEnd,
  onSave,
}: SettingsPanelProps) {
  const [selectedTz, setSelectedTz] = useState(timezone);
  const [start, setStart] = useState(toInputTime(workingHoursStart));
  const [end, setEnd] = useState(toInputTime(workingHoursEnd));
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");

  const dirty =
    selectedTz !== timezone ||
    start !== toInputTime(workingHoursStart) ||
    end !== toInputTime(workingHoursEnd);
  const invalidRange = start >= end;

  async function handleSave() {
    setStatus("saving");
    const update: ProfileUpdate = {};
    if (selectedTz !== timezone) update.timezone = selectedTz;
    if (start !== toInputTime(workingHoursStart)) update.working_hours_start = `${start}:00`;
    if (end !== toInputTime(workingHoursEnd)) update.working_hours_end = `${end}:00`;
    const ok = await onSave(update);
    setStatus(ok ? "saved" : "error");
  }

  return (
    <div className="rounded-[10px] border-[1.5px] border-dashed border-border px-6 py-5">
      <p className="mb-4 font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
        settings
      </p>

      <div className="grid grid-cols-1 gap-x-10 gap-y-5 sm:grid-cols-2">
        <div className="flex flex-col">
          <label
            htmlFor="settings-timezone"
            className="mb-2 block font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground"
          >
            Timezone
          </label>
          <select
            id="settings-timezone"
            value={selectedTz}
            onChange={(e) => {
              setSelectedTz(e.target.value);
              setStatus("idle");
            }}
            className="w-full rounded-[6px] border border-border bg-background px-2.5 py-2 text-[13px]"
          >
            {TIMEZONES.map((tz) => (
              <option key={tz} value={tz}>
                {tz.replace(/_/g, " ")}
              </option>
            ))}
          </select>

          {selectedTz !== DETECTED_TIMEZONE && (
            <button
              type="button"
              onClick={() => {
                setSelectedTz(DETECTED_TIMEZONE);
                setStatus("idle");
              }}
              className="mt-2 self-start font-mono text-[10px] text-muted-foreground underline hover:text-foreground"
            >
              Use detected: {DETECTED_TIMEZONE.replace(/_/g, " ")}
            </button>
          )}
        </div>

        <div className="flex flex-col">
          <p className="mb-2 block font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
            Working hours
          </p>
          <div className="flex items-center gap-2">
            <input
              type="time"
              aria-label="Working hours start"
              value={start}
              onChange={(e) => {
                setStart(e.target.value);
                setStatus("idle");
              }}
              className="w-full rounded-[6px] border border-border bg-background px-2.5 py-2 text-[13px]"
            />
            <span className="text-muted-foreground">–</span>
            <input
              type="time"
              aria-label="Working hours end"
              value={end}
              onChange={(e) => {
                setEnd(e.target.value);
                setStatus("idle");
              }}
              className="w-full rounded-[6px] border border-border bg-background px-2.5 py-2 text-[13px]"
            />
          </div>
          {invalidRange && (
            <p className="mt-2 text-[11px] text-destructive">Start has to be before end.</p>
          )}
        </div>
      </div>

      <button
        type="button"
        onClick={handleSave}
        disabled={status === "saving" || !dirty || invalidRange}
        className="mt-5 w-full rounded-[6px] border border-border bg-foreground py-2 text-[13px] font-medium text-background transition-opacity disabled:opacity-40"
      >
        {status === "saving" ? "Saving…" : status === "saved" ? "Saved" : "Save"}
      </button>
      {status === "error" && (
        <p className="mt-2 text-[11px] text-destructive">Couldn&apos;t save — try again.</p>
      )}
    </div>
  );
}
