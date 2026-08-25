import type { NextConfig } from "next";

// Ships to GitHub Pages — a static host, no Node server. `output: 'export'`
// forces every route to be pre-renderable to plain HTML/CSS/JS at build
// time; no server components/actions/ISR in this app (see plan Phase 1's
// "Next.js static export" decision).
//
// basePath: this repo isn't named `izaanqaiser.github.io`, so GitHub Pages
// serves it at izaanqaiser.github.io/bet/, not the domain root — found the
// hard way when a real registration link 404'd (Phase 4's live test).
// Without this, every next/link href and generated asset URL is built
// assuming it's served from `/`, which silently breaks routing and CSS/JS
// loading the moment it's actually deployed. Drop this once the plan's
// manual setup step 1 (a real custom domain, served from its own root) is
// live — every other `izaanqaiser.github.io` / `izaanqaiser.github.io/bet`
// reference in this codebase (scripts/deploy.sh's WEB_ORIGIN,
// scripts/approve_waitlist.py's WEB_BASE_URL default) needs to move to
// that domain in the same pass.
const nextConfig: NextConfig = {
  output: "export",
  basePath: "/bet",
  images: { unoptimized: true }, // next/image's optimizer needs a server; static export has none
};

export default nextConfig;
