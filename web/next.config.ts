import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // deck.gl and maplibre ship untranspiled ESM in places; let Next handle them.
  transpilePackages: ["deck.gl", "@deck.gl/react", "@deck.gl/layers", "@deck.gl/core"],
};

export default nextConfig;
