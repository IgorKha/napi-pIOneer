import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// В проде фронт отдаёт core под /pioneer/ (за штатным nginx платы).
export default defineConfig({
  base: "/pioneer/",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/pioneer/api": "http://127.0.0.1:8100",
      "/pioneer/ws": { target: "ws://127.0.0.1:8100", ws: true },
    },
  },
});
