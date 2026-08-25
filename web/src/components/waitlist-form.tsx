"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";

// ITU E.164: a leading +, then 1-15 digits, first digit non-zero — same
// rule registration-svc validates server-side (this is just faster
// feedback, not the real gate).
const E164_RE = /^\+[1-9]\d{1,14}$/;

type Status = "idle" | "submitting" | "joined" | "error";

export function WaitlistForm() {
  const [phone, setPhone] = useState("");
  const [name, setName] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    const trimmed = phone.trim();
    if (!E164_RE.test(trimmed)) {
      setError("Enter your number in E.164 format, e.g. +15551234567");
      return;
    }

    const baseUrl = process.env.NEXT_PUBLIC_REGISTRATION_SVC_URL;
    if (!baseUrl) {
      setError("Waitlist isn't wired up yet — try again shortly.");
      return;
    }

    setStatus("submitting");
    try {
      const res = await fetch(`${baseUrl}/waitlist/join`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone_e164: trimmed, name: name.trim() || null }),
      });
      if (!res.ok) throw new Error(`request failed: ${res.status}`);
      setStatus("joined");
    } catch {
      setStatus("error");
      setError("Something went wrong. Try again in a moment.");
    }
  }

  if (status === "joined") {
    return (
      <>
        <h1 className="mb-3 font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
          You&apos;re on the list.
        </h1>
        <p className="max-w-[42ch] text-base leading-relaxed text-muted-foreground">
          We&apos;ll text {phone.trim()} when you&apos;re approved, with a link to finish setup.
        </p>
      </>
    );
  }

  return (
    <>
      <h1 className="mb-3 font-serif text-[clamp(28px,4vw,38px)] leading-[1.05] tracking-[-0.02em]">
        Join the waitlist.
      </h1>
      <p className="mb-8 max-w-[42ch] text-base leading-relaxed text-muted-foreground">
        Leave your number. We&apos;ll text you when you&apos;re approved, with a link to connect
        your calendar and finish setup.
      </p>

      <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="phone" className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
            Phone number
          </label>
          <input
            id="phone"
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            placeholder="+15551234567"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            required
            className="h-10 rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="name" className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
            Name <span className="normal-case text-muted-foreground/70">(optional)</span>
          </label>
          <input
            id="name"
            type="text"
            autoComplete="name"
            placeholder="Sarah"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="h-10 rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          />
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <Button type="submit" disabled={status === "submitting"} className="mt-2 self-start">
          {status === "submitting" ? "Joining…" : "Join the waitlist"}
        </Button>
      </form>
    </>
  );
}
