import type { NextConfig } from "next";

// Ships to GitHub Pages — a static host, no Node server. `output: 'export'`
// forces every route to be pre-renderable to plain HTML/CSS/JS at build
// time; no server components/actions/ISR in this app (see plan Phase 1's
// "Next.js static export" decision).
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true }, // next/image's optimizer needs a server; static export has none
};

export default nextConfig;
