"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { MemoryList } from "@/components/memory-list";
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
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Polls rather than loading once: real feedback — the dashboard is
  // meant to reflect what the agent is doing right now, not a snapshot
  // from whenever the page happened to load.
  const POLL_INTERVAL_MS = 8000;

  useEffect(() => {
    if (!token || !baseUrl) return;
    let cancelled = false;

    async function load() {
      const t = token as string;
      const [itemsRes, profileRes] = await Promise.all([
        authedFetch("/me/items", t),
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
      if (!itemsRes.ok || !profileRes.ok) {
        if (!cancelled) setLoadError("Couldn't load your data. Try refreshing.");
        return;
      }
      const [itemsBody, profileBody] = await Promise.all([itemsRes.json(), profileRes.json()]);
      if (cancelled) return;
      setLoadError(null);
      setItems(itemsBody);
      setProfile(profileBody);
    }

    load();
    const interval = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
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
    setProfile(null);
    try {
      localStorage.removeItem(SESSION_KEY);
    } catch {
      // ignore
    }
  }

  async function deleteItem(itemId: string) {
    if (!token || !items) return;
    const target =
      items.in_progress.find((r) => r.id === itemId) ??
      items.committed.find((r) => r.id === itemId) ??
      items.other.find((r) => r.id === itemId);
    if (!window.confirm(`Remove "${target?.title ?? "this"}"? This can't be undone.`)) {
      return;
    }

    // Optimistic, but rolled back on failure (below) — silently reappearing
    // hours later on the next poll with no explanation was confusing; a
    // real failure now restores the item and says so immediately.
    const previous = items;
    setItems({
      in_progress: items.in_progress.filter((r) => r.id !== itemId),
      committed: items.committed.filter((r) => r.id !== itemId),
      other: items.other.filter((r) => r.id !== itemId),
    });
    try {
      const res = await authedFetch(`/me/items/${itemId}`, token, { method: "DELETE" });
      if (!res.ok) throw new Error(`delete failed: ${res.status}`);
    } catch {
      setItems(previous);
      setLoadError("Couldn't remove that — try again.");
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

  const timeZone = profile?.timezone ?? "UTC";

  return (
    <>
      <div className="mb-8 flex items-baseline justify-between gap-6">
        <h1 className="font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          What bet&apos;s tracking.
        </h1>
        <button
          onClick={logout}
          className="shrink-0 font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground"
        >
          Log out
        </button>
      </div>

      {loadError && <p className="mb-6 text-sm text-destructive">{loadError}</p>}

      {!items || !profile ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <MemoryList
          inProgress={items.in_progress}
          committed={items.committed}
          timeZone={timeZone}
          onDelete={deleteItem}
        />
      )}
    </>
  );
}
