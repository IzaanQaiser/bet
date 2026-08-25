"use client";

import { useState, type FormEvent } from "react";
import { useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";

// JWTs are signed, not encrypted — the payload is plain base64url, so this
// is a read-only convenience for display ("confirm it's you" without
// re-asking for the number), not a trust decision. The server verifies
// the signature for real on every actual request.
function decodePhoneFromToken(token: string): string | null {
  try {
    const payload = token.split(".")[1];
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const claims = JSON.parse(atob(padded));
    return typeof claims.phone_e164 === "string" ? claims.phone_e164 : null;
  } catch {
    return null;
  }
}

type Stage = "start" | "otp" | "connecting";

export function RegisterFlow() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token");
  const phone = token ? decodePhoneFromToken(token) : null;

  const [stage, setStage] = useState<Stage>("start");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const baseUrl = process.env.NEXT_PUBLIC_REGISTRATION_SVC_URL;

  if (!token || !phone) {
    return (
      <>
        <h1 className="mb-3 font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          This link isn&apos;t valid.
        </h1>
        <p className="max-w-[42ch] text-base leading-relaxed text-muted-foreground">
          Registration links come from a text after you&apos;re approved off the waitlist. If
          yours expired, ask to be re-approved.
        </p>
      </>
    );
  }

  async function sendCode() {
    if (!baseUrl) {
      setError("Registration isn't wired up yet, try again shortly.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${baseUrl}/register/verify-start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
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
      const res = await fetch(`${baseUrl}/register/verify-otp`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, code }),
      });
      if (!res.ok) {
        setError("That code didn't work. Check it and try again.");
        setSubmitting(false);
        return;
      }
      const { oauth_session_token } = await res.json();
      setStage("connecting");
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      const params = new URLSearchParams({ token: oauth_session_token, timezone });
      // Full top-level navigation, not fetch or next/router — this crosses
      // to registration-svc's own origin (a different domain from this
      // static site), and the browser needs to follow its 302 chain
      // through to Google's actual consent screen.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.href = `${baseUrl}/register/oauth-start?${params.toString()}`;
    } catch {
      setError("Something went wrong. Try again.");
      setSubmitting(false);
    }
  }

  if (stage === "connecting") {
    return (
      <>
        <h1 className="mb-3 font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          Connecting your calendar.
        </h1>
        <p className="max-w-[42ch] text-base leading-relaxed text-muted-foreground">
          Redirecting to Google&hellip;
        </p>
      </>
    );
  }

  if (stage === "otp") {
    return (
      <>
        <h1 className="mb-3 font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          Enter the code.
        </h1>
        <p className="mb-8 max-w-[42ch] text-base leading-relaxed text-muted-foreground">
          We texted a code to {phone}.
        </p>
        <form onSubmit={verifyCode} className="flex flex-col gap-4" noValidate>
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="code"
              className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground"
            >
              Code
            </label>
            <input
              id="code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="123456"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              required
              className="h-10 rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <Button type="submit" disabled={submitting} className="mt-2 self-start">
            {submitting ? "Verifying…" : "Verify"}
          </Button>
        </form>
      </>
    );
  }

  return (
    <>
      <h1 className="mb-3 font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
        Confirm it&apos;s you.
      </h1>
      <p className="mb-8 max-w-[42ch] text-base leading-relaxed text-muted-foreground">
        We&apos;ll text a code to {phone} to confirm before connecting your calendar.
      </p>
      {error && <p className="mb-4 text-sm text-destructive">{error}</p>}
      <Button onClick={sendCode} disabled={submitting} className="self-start">
        {submitting ? "Sending…" : "Send code"}
      </Button>
    </>
  );
}
