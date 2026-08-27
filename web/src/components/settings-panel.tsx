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
    <div className="rounded-[10px] border-[1.5px] border-dashed border-border px-[18px] pb-[14px] pt-4">
      <p className="mb-[3px] font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
        settings
      </p>
      <p className="mb-3 text-xs text-muted-foreground">timezone and working hours</p>

      <label
        htmlFor="settings-timezone"
        className="mb-1 block font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground"
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
        className="mb-1.5 w-full max-w-[280px] rounded-[6px] border border-border bg-background px-2 py-1.5 text-[13px]"
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
          className="mb-3 block font-mono text-[10px] text-muted-foreground underline hover:text-foreground"
        >
          Use detected: {DETECTED_TIMEZONE.replace(/_/g, " ")}
        </button>
      )}

      <p className="mb-1 block font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
        Working hours
      </p>
      <p className="mb-1.5 max-w-[42ch] text-[11px] leading-snug text-muted-foreground">
        When you&apos;re free to work — this is what idea suggestions and reminders are scored
        against.
      </p>
      <div className="mb-1.5 flex max-w-[280px] items-center gap-2">
        <input
          type="time"
          aria-label="Working hours start"
          value={start}
          onChange={(e) => {
            setStart(e.target.value);
            setStatus("idle");
          }}
          className="w-full rounded-[6px] border border-border bg-background px-2 py-1.5 text-[13px]"
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
          className="w-full rounded-[6px] border border-border bg-background px-2 py-1.5 text-[13px]"
        />
      </div>
      {invalidRange && (
        <p className="mb-1.5 text-[11px] text-destructive">Start has to be before end.</p>
      )}

      <button
        type="button"
        onClick={handleSave}
        disabled={status === "saving" || !dirty || invalidRange}
        className="mt-1.5 w-full max-w-[280px] rounded-[6px] border border-border bg-foreground py-1.5 text-[13px] font-medium text-background transition-opacity disabled:opacity-40"
      >
        {status === "saving" ? "Saving…" : status === "saved" ? "Saved" : "Save"}
      </button>
      {status === "error" && (
        <p className="mt-1.5 text-[11px] text-destructive">Couldn&apos;t save — try again.</p>
      )}
    </div>
  );
}
