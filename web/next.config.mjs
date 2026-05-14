/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Turbopack alias: react-plotly.js expects 'plotly.js/dist/plotly' (full build)
  // but we ship plotly.js-dist-min — map them so production builds succeed.
  turbopack: {
    resolveAlias: {
      "plotly.js/dist/plotly": "plotly.js-dist-min",
    },
  },
};

export default nextConfig;
