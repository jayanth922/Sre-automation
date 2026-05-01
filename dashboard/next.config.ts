import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    proxyTimeout: 180000,
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
