import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    proxyTimeout: 180000,
  },
  // Use rewrites to proxy API requests to the backend
  async rewrites() {
    const api = process.env.API_URL || "http://localhost:8080";
    return [
      // Full /api passthrough
      {
        source: "/api/:path*",
        destination: `${api}/api/:path*`,
      },
      // Auth endpoints
      {
        source: "/auth/:path*",
        destination: `${api}/auth/:path*`,
      },
      // Proxy metrics and agent state endpoints
      {
        source: "/metrics/:path*",
        destination: `${api}/metrics/:path*`,
      },
      {
        source: "/agent/:path*",
        destination: `${api}/agent/:path*`,
      },
    ];
  },
};

export default nextConfig;
