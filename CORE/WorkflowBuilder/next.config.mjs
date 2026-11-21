/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  eslint: {
    ignoreDuringBuilds: true,
  },
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
  webpack: (config, { isServer }) => {
    // Optimize module resolution for circular dependencies
    config.optimization = {
      ...config.optimization,
      moduleIds: 'deterministic',
    };

    // Add fallbacks for node modules if needed
    if (!isServer) {
      config.resolve.fallback = {
        ...config.resolve.fallback,
      };
    }

    return config;
  },
}

export default nextConfig