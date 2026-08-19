/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emits a minimal self-contained server bundle, so the runtime image does
  // not need node_modules copied into it.
  output: "standalone",
  reactStrictMode: true,
};

export default nextConfig;
