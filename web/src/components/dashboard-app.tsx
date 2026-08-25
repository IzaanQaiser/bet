"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Bubble } from "@/components/hero-thread";
import { Button } from "@/components/ui/button";

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

interface MessageRow {
  direction: "in" | "out";
  body: string;
  created_at: string;
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

function LedgerRow({ row, muted = false }: { row: ItemRow; muted?: boolean }) {
  return (
    <div className="flex flex-col gap-1 border-t border-dashed border-border py-3 first:border-t-0">
      <div className="flex items-baseline justify-between gap-4">
        <span className={`text-sm ${muted ? "text-muted-foreground" : "text-foreground"}`}>
          {row.title}
        </span>
        {row.state && (
          <span className="font-mono text-[0.6875rem] text-muted-foreground">{row.state}</span>
        )}
      </div>
      {row.summary && !muted && (
        <p className="text-xs leading-relaxed text-muted-foreground">{row.summary}</p>
      )}
      {row.pending_fields && row.pending_fields.length > 0 && (
        <p className="font-mono text-[0.6875rem] text-muted-foreground">
          waiting on: {row.pending_fields.join(", ")}
        </p>
      )}
      {row.due_at && (
        <p className="font-mono text-[0.6875rem] text-muted-foreground">
          due {new Date(row.due_at).toLocaleString()}
          {row.calendar_event_id ? " — on your calendar" : ""}
        </p>
      )}
    </div>
  );
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
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [stage, setStage] = useState<"phone" | "otp">("phone");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function sendCode(e: FormEvent) {
    e.preventDefault();
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
        body: JSON.stringify({ phone_e164: phone.trim() }),
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
    if (!baseUrl) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${baseUrl}/auth/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone_e164: phone.trim(), code }),
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
          We texted a code to {phone.trim()}.
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
        inputMode="tel"
        autoComplete="tel"
        placeholder="+15551234567"
        value={phone}
        onChange={(e) => setPhone(e.target.value)}
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
  // Lazy initializer, not an effect + setState: this runs once, during
  // React's first client render (hydration), when window/localStorage
  // actually exist — the build-time prerender (no window) and any
  // stored-session client render can genuinely disagree, which is an
  // accepted tradeoff for an auth-gated page that isn't indexed/SEO
  // content anyway; React just re-renders once to the real client truth.
  const [token, setToken] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    try {
      return localStorage.getItem(SESSION_KEY);
    } catch {
      return null;
    }
  });
  const [items, setItems] = useState<ItemsResponse | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [suggestions, setSuggestions] = useState<SuggestionRow[]>([]);
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !baseUrl) return;
    let cancelled = false;

    async function load() {
      const t = token as string;
      const [itemsRes, messagesRes, suggestionsRes, profileRes] = await Promise.all([
        authedFetch("/me/items", t),
        authedFetch("/me/messages", t),
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
      if (!itemsRes.ok || !messagesRes.ok || !suggestionsRes.ok || !profileRes.ok) {
        if (!cancelled) setLoadError("Couldn't load your data. Try refreshing.");
        return;
      }
      const [itemsBody, messagesBody, suggestionsBody, profileBody] = await Promise.all([
        itemsRes.json(),
        messagesRes.json(),
        suggestionsRes.json(),
        profileRes.json(),
      ]);
      if (cancelled) return;
      setItems(itemsBody);
      setMessages(messagesBody.messages);
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
    setMessages([]);
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

  return (
    <>
      <div className="mb-8 flex items-baseline justify-between">
        <h1 className="font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          Dashboard.
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
          <Section title="In progress">
            {items.in_progress.length === 0 ? (
              <p className="text-sm text-muted-foreground">Nothing in progress.</p>
            ) : (
              items.in_progress.map((row) => <LedgerRow key={row.id} row={row} />)
            )}
          </Section>

          <Section title="Committed">
            {items.committed.length === 0 ? (
              <p className="text-sm text-muted-foreground">Nothing committed yet.</p>
            ) : (
              items.committed.map((row) => <LedgerRow key={row.id} row={row} />)
            )}
          </Section>

          {items.other.length > 0 && (
            <Section title="Other">
              {items.other.map((row) => (
                <LedgerRow key={row.id} row={row} muted />
              ))}
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

          <Section title="Messages">
            <div className="flex max-h-[420px] flex-col gap-[9px] overflow-y-auto rounded-[10px] border border-border p-4">
              {messages.length === 0 ? (
                <p className="text-sm text-muted-foreground">No messages yet.</p>
              ) : (
                messages.map((m, i) => (
                  <Bubble
                    key={i}
                    item={{
                      kind: "bubble",
                      id: String(i),
                      // messages.direction is system-relative ("in" = the
                      // user's own text arriving, "out" = the bot's own
                      // send) — Bubble's dir is speaker-relative ("out" =
                      // the user's bubble, right-aligned), the opposite
                      // convention, same as the hero mockup's own copy.
                      dir: m.direction === "out" ? "in" : "out",
                      text: m.body,
                      showStamp: false,
                    }}
                    spacing={i > 0 ? "mt-[9px]" : ""}
                  />
                ))
              )}
            </div>
          </Section>

          <Section title="Profile">
            <ProfilePanel token={token} profile={profile} />
          </Section>
        </>
      )}
    </>
  );
}
