/** @type {import('next').NextConfig} */
// The admin console lives under its own base path (default /agent-factory) so it can share the host with the
// recruiter UI (/ats) and Grafana (/grafana) without route clashes: pages, assets (/agent-factory/_next) and the API
// proxy (/agent-factory/api/backend) all stay under it. Next.js bakes basePath at BUILD time, so to change it the
// image must be built with NEXT_BASE_PATH set (the Dockerfile forwards it); the chart's route must match.
const basePath = process.env.NEXT_BASE_PATH ?? '/agent-factory';

const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  productionBrowserSourceMaps: false,   // never ship source maps (they would expose source code and comments)
  basePath,
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
};

module.exports = nextConfig;
