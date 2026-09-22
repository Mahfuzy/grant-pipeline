import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built into dist/ and served by the API at /admin. `npm run dev` proxies API calls to
// a local `fundscout api` on port 8000.
const api = "http://127.0.0.1:8000";

export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      ["/grants", "/changes", "/sources", "/review", "/taxonomy", "/health"].map((p) => [p, api]),
    ),
  },
});
