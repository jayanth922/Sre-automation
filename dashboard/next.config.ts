import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Disable Turbopack — use stable webpack bundler in dev
  // (Turbopack has a stale-module-graph bug with newly added files)
  experimental: {
    turbo: {
      enabled: false,
    },
  },
  // Use rewrites to proxy API requests to the backend
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.API_URL || "http://localhost:8080"}/api/:path*`,
      },
      // Also proxy auth endpoints if they are at root
      {
        source: "/auth/:path*",
        destination: `${process.env.API_URL || "http://localhost:8080"}/auth/:path*`,
      },
      // Proxy metrics and agent state endpoints
      {
        source: "/metrics/:path*",
        destination: `${process.env.API_URL || "http://localhost:8080"}/metrics/:path*`,
      },
      {
        source: "/agent/:path*",
        destination: `${process.env.API_URL || "http://localhost:8080"}/agent/:path*`,
      },
    ];
  },
};

export default nextConfig;
