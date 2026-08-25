import Link from "next/link";
import { DashboardApp } from "@/components/dashboard-app";

export const metadata = {
  title: "Dashboard",
};

export default function DashboardPage() {
  return (
    <div className="mx-auto flex min-h-screen max-w-[720px] flex-col px-8 py-16">
      <Link
        href="/"
        className="mb-[18px] w-fit font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground"
      >
        bet
      </Link>
      <DashboardApp />
    </div>
  );
}
