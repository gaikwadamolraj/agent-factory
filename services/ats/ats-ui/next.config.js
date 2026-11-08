/** @type {import('next').NextConfig} */
// The app lives under a base path (default /ats) so it can share a host with other apps without route clashes:
// pages, assets (/ats/_next) and the API proxy (/ats/api/backend) all stay under it. Build-time setting.
const basePath = process.env.NEXT_BASE_PATH ?? '/ats';

const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  productionBrowserSourceMaps: false,   // never ship source maps (they would expose source code and comments)
  basePath,
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
};

module.exports = nextConfig;
