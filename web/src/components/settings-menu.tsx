"use client";

import { useState } from "react";
import { Popover } from "@base-ui/react/popover";

interface SettingsMenuProps {
  timezone: string;
  onSave: (timezone: string) => Promise<boolean>;
}

// Every real IANA zone name, natively — no curated city list, no external
// geocoding API. Detection itself (below) reads the browser/OS's own
// configured zone directly, same Intl call already used in the
// registration flow (web-division plan Phase 4) — no location permission
// prompt, no GPS.
const TIMEZONES = Intl.supportedValuesOf("timeZone");
const DETECTED_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

function GearIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path
        d="M19.4 13a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V19a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 17.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 13a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 7a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 2.68 1.65 1.65 0 0 0 10 1.18V1a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 7c.15.36.44.65.8.82.2.1.42.15.65.15H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"
        stroke="currentColor"
        strokeWidth="1.5"
      />
    </svg>
  );
}

export function SettingsMenu({ timezone, onSave }: SettingsMenuProps) {
  const [selected, setSelected] = useState(timezone);
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");

  async function handleSave() {
    setStatus("saving");
    const ok = await onSave(selected);
    setStatus(ok ? "saved" : "error");
  }

  return (
    <Popover.Root
      onOpenChange={(open) => {
        if (open) {
          setSelected(timezone);
          setStatus("idle");
        }
      }}
    >
      <Popover.Trigger
        aria-label="Settings"
        className="flex h-6 w-6 items-center justify-center rounded-full text-muted-foreground hover:text-foreground"
      >
        <GearIcon />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner side="bottom" align="start" sideOffset={8}>
          <Popover.Popup className="w-[260px] rounded-[10px] border-[1.5px] border-dashed border-border bg-background px-[18px] py-4 shadow-sm outline-none">
            <p className="mb-3 font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
              Settings
            </p>

            <label
              htmlFor="settings-timezone"
              className="mb-1 block font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground"
            >
              Timezone
            </label>
            <select
              id="settings-timezone"
              value={selected}
              onChange={(e) => {
                setSelected(e.target.value);
                setStatus("idle");
              }}
              className="mb-1.5 w-full rounded-[6px] border border-border bg-background px-2 py-1.5 text-[13px]"
            >
              {TIMEZONES.map((tz) => (
                <option key={tz} value={tz}>
                  {tz.replace(/_/g, " ")}
                </option>
              ))}
            </select>

            {selected !== DETECTED_TIMEZONE && (
              <button
                type="button"
                onClick={() => {
                  setSelected(DETECTED_TIMEZONE);
                  setStatus("idle");
                }}
                className="mb-3 block font-mono text-[10px] text-muted-foreground underline hover:text-foreground"
              >
                Use detected: {DETECTED_TIMEZONE.replace(/_/g, " ")}
              </button>
            )}

            <button
              type="button"
              onClick={handleSave}
              disabled={status === "saving" || selected === timezone}
              className="w-full rounded-[6px] border border-border bg-foreground py-1.5 text-[13px] font-medium text-background transition-opacity disabled:opacity-40"
            >
              {status === "saving" ? "Saving…" : status === "saved" ? "Saved" : "Save"}
            </button>
            {status === "error" && (
              <p className="mt-1.5 text-[11px] text-destructive">Couldn&apos;t save — try again.</p>
            )}
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
