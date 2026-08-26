"use client";

import { GoogleCalendarIcon } from "@/components/calendar-card";

export interface WeekCalendarItem {
  id: string;
  title: string;
  due_at: string;
  effort_minutes: number | null;
}

interface WeekCalendarProps {
  items: WeekCalendarItem[];
  timeZone: string;
  onDelete?: (id: string) => void;
}

const HOUR_PX = 44;
const DAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// All calendar-day math below works on "YYYY-MM-DD" strings, never on Date
// objects constructed from them — that's what keeps it timezone-correct.
// A string is derived from a real instant via Intl with the user's actual
// timeZone (never the browser's ambient one, same discipline the rest of
// this dashboard already follows); once it's a plain calendar-date string,
// day-of-week/week-boundary arithmetic is done by anchoring it at
// T00:00:00Z purely as a UTC scratchpad for calendar math — never mixed
// back into a real wall-clock/timezone conversion.
function localDateString(iso: string, timeZone: string): string {
  return new Date(iso).toLocaleDateString("en-CA", { timeZone });
}

function localHourMinute(iso: string, timeZone: string): { hour: number; minute: number } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour: "numeric",
    minute: "numeric",
    hourCycle: "h23",
  }).formatToParts(new Date(iso));
  const hour = Number(parts.find((p) => p.type === "hour")?.value ?? "0");
  const minute = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
  return { hour, minute };
}

function dateStringWeekday(dateStr: string): number {
  return new Date(`${dateStr}T00:00:00Z`).getUTCDay();
}

function addDaysToDateString(dateStr: string, days: number): string {
  const d = new Date(`${dateStr}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function hourLabel(h: number): string {
  if (h === 0) return "12 AM";
  if (h === 12) return "12 PM";
  return h < 12 ? `${h} AM` : `${h - 12} PM`;
}

export function WeekCalendar({ items, timeZone, onDelete }: WeekCalendarProps) {
  const todayStr = new Date().toLocaleDateString("en-CA", { timeZone });

  const withDayStr = items.map((item) => ({
    item,
    dayStr: localDateString(item.due_at, timeZone),
  }));

  // Which week to show: the week containing the nearest upcoming item, so
  // a real committed item never just silently disappears because it isn't
  // in the literal current calendar week. Falls back to the most recent
  // past item, then to the current week when there are no items at all.
  const upcoming = withDayStr.filter((r) => r.dayStr >= todayStr).sort((a, b) => a.dayStr.localeCompare(b.dayStr));
  const past = withDayStr.filter((r) => r.dayStr < todayStr).sort((a, b) => b.dayStr.localeCompare(a.dayStr));
  const anchorDayStr = upcoming[0]?.dayStr ?? past[0]?.dayStr ?? todayStr;

  const weekStart = addDaysToDateString(anchorDayStr, -dateStringWeekday(anchorDayStr));
  const weekDayStrs = Array.from({ length: 7 }, (_, i) => addDaysToDateString(weekStart, i));

  const itemsThisWeek = withDayStr
    .filter((r) => weekDayStrs.includes(r.dayStr))
    .map((r) => {
      const { hour, minute } = localHourMinute(r.item.due_at, timeZone);
      const durationHours = (r.item.effort_minutes ?? 30) / 60;
      return { ...r, hour, minute, durationHours, dayIndex: weekDayStrs.indexOf(r.dayStr) };
    });

  const hours = itemsThisWeek.map((r) => r.hour + r.minute / 60);
  const endHours = itemsThisWeek.map((r) => r.hour + r.minute / 60 + r.durationHours);
  const rangeStart = Math.max(0, Math.floor(Math.min(7, ...(hours.length ? hours : [7]))));
  const rangeEnd = Math.min(24, Math.ceil(Math.max(21, ...(endHours.length ? endHours : [21]))));
  const hourList = Array.from({ length: rangeEnd - rangeStart }, (_, i) => rangeStart + i);
  const totalHeight = (rangeEnd - rangeStart) * HOUR_PX;

  const nowHour = (() => {
    const { hour, minute } = localHourMinute(new Date().toISOString(), timeZone);
    return hour + minute / 60;
  })();
  const todayColumnIndex = weekDayStrs.indexOf(todayStr);

  return (
    <div className="overflow-x-auto rounded-[10px] border border-border">
      <div className="min-w-[600px]">
        <div className="flex items-center gap-[7px] border-b border-border bg-muted/50 px-3 py-2">
          <GoogleCalendarIcon />
          <span className="font-mono text-[10px] font-medium tracking-[0.03em] text-muted-foreground">
            On your Google Calendar
          </span>
        </div>

        <div className="grid grid-cols-[44px_repeat(7,1fr)] border-b border-border">
          <div />
          {weekDayStrs.map((dayStr, i) => (
            <div
              key={dayStr}
              className={`border-l border-border py-1.5 text-center font-mono text-[10px] ${
                dayStr === todayStr
                  ? "font-semibold text-foreground shadow-[inset_0_-2px_0_var(--foreground)]"
                  : "text-muted-foreground"
              }`}
            >
              {DAY_LABELS[i]} <span className="text-muted-foreground">{Number(dayStr.slice(8))}</span>
            </div>
          ))}
        </div>

        <div className="relative grid grid-cols-[44px_repeat(7,1fr)]" style={{ height: totalHeight }}>
          <div className="relative">
            {hourList.map((h) => (
              <div
                key={h}
                className="absolute -translate-y-1/2 pr-1.5 text-right font-mono text-[9px] text-muted-foreground"
                style={{ top: (h - rangeStart) * HOUR_PX, right: 0 }}
              >
                {hourLabel(h)}
              </div>
            ))}
          </div>

          {weekDayStrs.map((dayStr, dayIndex) => (
            <div key={dayStr} className="relative border-l border-border">
              {hourList.map((h) => (
                <div
                  key={h}
                  className="absolute w-full border-t border-dashed border-border/70"
                  style={{ top: (h - rangeStart) * HOUR_PX }}
                />
              ))}
              {dayIndex === todayColumnIndex && nowHour >= rangeStart && nowHour <= rangeEnd && (
                <div
                  className="absolute z-[1] w-full border-t-[1.5px] border-retrieved"
                  style={{ top: (nowHour - rangeStart) * HOUR_PX }}
                  aria-hidden="true"
                />
              )}
              {itemsThisWeek
                .filter((r) => r.dayIndex === dayIndex)
                .map((r) => {
                  const top = (r.hour + r.minute / 60 - rangeStart) * HOUR_PX;
                  const height = Math.max(24, r.durationHours * HOUR_PX);
                  const due = new Date(r.item.due_at);
                  return (
                    <div
                      key={r.item.id}
                      role="img"
                      aria-label={`${r.item.title}, ${DAY_LABELS[dayIndex]} ${hourLabel(r.hour)}`}
                      className="absolute inset-x-[3px] overflow-hidden rounded-[6px] border border-border bg-background px-[7px] py-[3px]"
                      style={{ top, height }}
                    >
                      {onDelete && (
                        <button
                          type="button"
                          onClick={() => onDelete(r.item.id)}
                          aria-label={`Remove ${r.item.title}`}
                          className="absolute right-1 top-1 z-[1] text-[10px] text-muted-foreground/60 hover:text-destructive"
                        >
                          ×
                        </button>
                      )}
                      <p className="m-0 truncate pr-3 text-[11px] font-medium leading-tight">{r.item.title}</p>
                      <p className="m-0 font-mono text-[9px] leading-tight text-muted-foreground">
                        {due.toLocaleTimeString("en-US", { timeZone, hour: "numeric", minute: "2-digit" })}
                      </p>
                    </div>
                  );
                })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
