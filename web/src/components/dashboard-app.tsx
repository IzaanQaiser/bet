"use client";

import { useEffect, useState, type FormEvent } from "react";
import { CalendarCard } from "@/components/calendar-card";
import { HeroMemory, type MemoryRowData } from "@/components/hero-memory";
import { Button } from "@/components/ui/button";
import { digitsOnly, formatPhoneDisplay, toE164 } from "@/lib/phone";

const SESSION_KEY = "bet_dashboard_session";
const baseUrl = process.env.NEXT_PUBLIC_DASHBOARD_SVC_URL;

interface ItemRow {
  id: string;
  title: string;
  summary: string | null;
  state?: string;
  updated_at?: string;
  due_at?: string | null;
  calendar_event_id?: string | null;
  pending_fields?: string[] | null;
  last_message_at?: string | null;
}

interface ItemsResponse {
  in_progress: ItemRow[];
  committed: ItemRow[];
  other: ItemRow[];
}

interface SuggestionRow {
  id: string;
  title: string;
  outcome: string | null;
  sent_at: string;
  responded_at: string | null;
}

interface ProfileData {
  phone_e164: string;
  timezone: string;
  working_hours_start: string;
  working_hours_end: string;
}

async function authedFetch(path: string, token: string, init?: RequestInit) {
  return fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { ...init?.headers, Authorization: `Bearer ${token}` },
  });
}

function humanizeState(state: string | undefined): string {
  if (!state) return "";
  return state.toLowerCase().replace(/_/g, " ");
}

function shortDate(iso: string | null | undefined): string {
  if (!iso) return "committed";
  return new Date(iso).toLocaleDateString(undefined, { weekday: "short" });
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-10">
      <h2 className="mb-2 font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
        {title}
      </h2>
      {children}
    </section>
  );
}

function LoginForm({ onLoggedIn }: { onLoggedIn: (token: string) => void }) {
  const [phoneDigits, setPhoneDigits] = useState("");
  const [code, setCode] = useState("");
  const [stage, setStage] = useState<"phone" | "otp">("phone");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function sendCode(e: FormEvent) {
    e.preventDefault();
    const phoneE164 = toE164(phoneDigits);
    if (!phoneE164) {
      setError("Enter a 10-digit phone number");
      return;
    }
    if (!baseUrl) {
      setError("Dashboard isn't wired up yet, try again shortly.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${baseUrl}/auth/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone_e164: phoneE164 }),
      });
      if (res.status === 404) {
        setError("That number isn't registered yet — join the waitlist first.");
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      setStage("otp");
    } catch {
      setError("Couldn't send a code. Try again in a moment.");
    } finally {
      setSubmitting(false);
    }
  }

  async function verifyCode(e: FormEvent) {
    e.preventDefault();
    const phoneE164 = toE164(phoneDigits);
    if (!baseUrl || !phoneE164) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${baseUrl}/auth/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone_e164: phoneE164, code }),
      });
      if (!res.ok) {
        setError("That code didn't work. Check it and try again.");
        return;
      }
      const { session_token } = await res.json();
      onLoggedIn(session_token);
    } catch {
      setError("Something went wrong. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (stage === "otp") {
    return (
      <form onSubmit={verifyCode} className="flex flex-col gap-4" noValidate>
        <p className="text-base leading-relaxed text-muted-foreground">
          We texted a code to {formatPhoneDisplay(phoneDigits)}.
        </p>
        <input
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="123456"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          required
          className="h-10 w-full max-w-[220px] rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
        />
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Button type="submit" disabled={submitting} className="self-start">
          {submitting ? "Verifying…" : "Verify"}
        </Button>
      </form>
    );
  }

  return (
    <form onSubmit={sendCode} className="flex flex-col gap-4" noValidate>
      <input
        type="tel"
        inputMode="numeric"
        autoComplete="tel"
        placeholder="(555) 123-4567"
        value={formatPhoneDisplay(phoneDigits)}
        onChange={(e) => setPhoneDigits(digitsOnly(e.target.value))}
        required
        className="h-10 w-full max-w-[280px] rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
      />
      {error && <p className="text-sm text-destructive">{error}</p>}
      <Button type="submit" disabled={submitting} className="self-start">
        {submitting ? "Sending…" : "Send code"}
      </Button>
    </form>
  );
}

function ProfilePanel({ token, profile }: { token: string; profile: ProfileData }) {
  const [timezone, setTimezone] = useState(profile.timezone);
  const [start, setStart] = useState(profile.working_hours_start.slice(0, 5));
  const [end, setEnd] = useState(profile.working_hours_end.slice(0, 5));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaved(false);
    try {
      await authedFetch("/me/profile", token, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          timezone,
          working_hours_start: start,
          working_hours_end: end,
        }),
      });
      setSaved(true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={save} className="flex flex-wrap items-end gap-4">
      <div className="flex flex-col gap-1.5">
        <label className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
          Timezone
        </label>
        <input
          value={timezone}
          onChange={(e) => setTimezone(e.target.value)}
          className="h-9 w-[220px] rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
          Working hours
        </label>
        <div className="flex items-center gap-2">
          <input
            type="time"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className="h-9 rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          />
          <span className="text-muted-foreground">–</span>
          <input
            type="time"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className="h-9 rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          />
        </div>
      </div>
      <Button type="submit" variant="outline" disabled={saving}>
        {saving ? "Saving…" : saved ? "Saved" : "Save"}
      </Button>
    </form>
  );
}

export function DashboardApp() {
  // Lazy initializer, not an effect + setState — same reasoning as
  // register-flow.tsx: the build-time prerender and a real stored-session
  // client render can genuinely disagree once, an accepted tradeoff for
  // an auth-gated, non-indexed page.
  const [token, setToken] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    try {
      return localStorage.getItem(SESSION_KEY);
    } catch {
      return null;
    }
  });
  const [items, setItems] = useState<ItemsResponse | null>(null);
  const [suggestions, setSuggestions] = useState<SuggestionRow[]>([]);
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !baseUrl) return;
    let cancelled = false;

    async function load() {
      const t = token as string;
      const [itemsRes, suggestionsRes, profileRes] = await Promise.all([
        authedFetch("/me/items", t),
        authedFetch("/me/suggestions", t),
        authedFetch("/me/profile", t),
      ]);
      if (itemsRes.status === 401 || profileRes.status === 401) {
        if (!cancelled) {
          setToken(null);
          try {
            localStorage.removeItem(SESSION_KEY);
          } catch {
            // ignore
          }
        }
        return;
      }
      if (!itemsRes.ok || !suggestionsRes.ok || !profileRes.ok) {
        if (!cancelled) setLoadError("Couldn't load your data. Try refreshing.");
        return;
      }
      const [itemsBody, suggestionsBody, profileBody] = await Promise.all([
        itemsRes.json(),
        suggestionsRes.json(),
        profileRes.json(),
      ]);
      if (cancelled) return;
      setItems(itemsBody);
      setSuggestions(suggestionsBody.suggestions);
      setProfile(profileBody);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [token]);

  function handleLoggedIn(newToken: string) {
    setToken(newToken);
    try {
      localStorage.setItem(SESSION_KEY, newToken);
    } catch {
      // private browsing / storage disabled — session just won't survive a reload
    }
  }

  function logout() {
    setToken(null);
    setItems(null);
    setSuggestions([]);
    setProfile(null);
    try {
      localStorage.removeItem(SESSION_KEY);
    } catch {
      // ignore
    }
  }

  if (!token) {
    return (
      <>
        <h1 className="mb-3 font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          Log in.
        </h1>
        <p className="mb-8 max-w-[42ch] text-base leading-relaxed text-muted-foreground">
          Enter the number you registered with.
        </p>
        <LoginForm onLoggedIn={handleLoggedIn} />
      </>
    );
  }

  const memoryRows: MemoryRowData[] = items
    ? [
        ...items.in_progress.map((row) => ({
          key: row.id,
          title: row.title,
          status: humanizeState(row.state),
          visible: true,
          glow: false,
          retrieved: false,
        })),
        ...items.committed.map((row) => ({
          key: row.id,
          title: row.title,
          status: shortDate(row.due_at),
          visible: true,
          glow: false,
          retrieved: true,
        })),
      ]
    : [];

  const committedWithDates = items ? items.committed.filter((row) => row.due_at) : [];

  return (
    <>
      <div className="mb-8 flex items-baseline justify-between">
        <h1 className="font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          What bet&apos;s tracking.
        </h1>
        <button
          onClick={logout}
          className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground"
        >
          Log out
        </button>
      </div>

      {loadError && <p className="mb-6 text-sm text-destructive">{loadError}</p>}

      {!items || !profile ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <>
          {memoryRows.length === 0 ? (
            <div className="mb-10 rounded-[10px] border-[1.5px] border-dashed border-border px-[18px] py-4">
              <p className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
                agent memory
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                Nothing yet — text bet something and it&apos;ll show up here.
              </p>
            </div>
          ) : (
            <HeroMemory rows={memoryRows} />
          )}

          {committedWithDates.length > 0 && (
            <Section title="On your calendar">
              <div className="flex flex-wrap gap-4">
                {committedWithDates.map((row) => {
                  const due = new Date(row.due_at as string);
                  return (
                    <CalendarCard
                      key={row.id}
                      variant="booked"
                      activeDay={due.getDay()}
                      title={row.title}
                      time={due.toLocaleTimeString(undefined, {
                        hour: "numeric",
                        minute: "2-digit",
                      })}
                      tag="On your Google Calendar"
                    />
                  );
                })}
              </div>
            </Section>
          )}

          {suggestions.length > 0 && (
            <Section title="Suggestions">
              {suggestions.map((s) => (
                <div
                  key={s.id}
                  className="flex items-baseline justify-between gap-4 border-t border-dashed border-border py-2 text-sm first:border-t-0"
                >
                  <span>{s.title}</span>
                  <span className="font-mono text-[0.6875rem] text-muted-foreground">
                    {s.outcome ?? "pending"}
                  </span>
                </div>
              ))}
            </Section>
          )}

          <Section title="Profile">
            <ProfilePanel token={token} profile={profile} />
          </Section>
        </>
      )}
    </>
  );
}
