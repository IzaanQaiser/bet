"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { digitsOnly, formatPhoneDisplay, toE164 } from "@/lib/phone";

type Status = "idle" | "submitting" | "joined" | "error";

export function WaitlistForm() {
  const [phoneDigits, setPhoneDigits] = useState("");
  const [name, setName] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    const phoneE164 = toE164(phoneDigits);
    if (!phoneE164) {
      setError("Enter a 10-digit phone number");
      return;
    }
    const trimmedName = name.trim();
    if (!trimmedName) {
      setError("Enter your name");
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
        body: JSON.stringify({ phone_e164: phoneE164, name: trimmedName }),
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
          We&apos;ll text {formatPhoneDisplay(phoneDigits)} when you&apos;re approved, with a link
          to finish setup.
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
            inputMode="numeric"
            autoComplete="tel"
            placeholder="(555) 123-4567"
            value={formatPhoneDisplay(phoneDigits)}
            onChange={(e) => setPhoneDigits(digitsOnly(e.target.value))}
            required
            className="h-10 rounded-[10px] border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="name" className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
            Name
          </label>
          <input
            id="name"
            type="text"
            autoComplete="name"
            placeholder="Sarah"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
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
