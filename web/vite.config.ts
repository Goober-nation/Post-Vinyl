import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// The app's Origin check (CSRF) only accepts its own origin, so the dev proxy
// sends that one. PV_API must equal the app's PV_PUBLIC_ORIGIN.
const api = process.env.PV_API ?? "http://localhost:8000";

export default defineConfig({
  plugins: [tailwindcss()],
  server: {
    proxy: {
      "/api": {
        target: api,
        changeOrigin: true,
        configure: (proxy) => proxy.on("proxyReq", (req) => req.setHeader("origin", api)),
      },
    },
  },
});
