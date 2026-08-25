import Link from "next/link";
import { WaitlistForm } from "@/components/waitlist-form";

export const metadata = {
  title: "Join the waitlist",
};

export default function WaitlistPage() {
  return (
    <div className="mx-auto flex min-h-screen max-w-[560px] flex-col justify-center px-8 py-16">
      <Link
        href="/"
        className="mb-[18px] w-fit font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground"
      >
        bet
      </Link>
      <WaitlistForm />
    </div>
  );
}
