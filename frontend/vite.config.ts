import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// В проде фронт отдаёт core под /pioneer/ (за штатным nginx платы).
// PIONEER_CORE / PIONEER_UI_PORT — вторая пара dev-серверов рядом с первой.
declare const process: { env: Record<string, string | undefined> }; // без @types/node
const core = process.env.PIONEER_CORE ?? "127.0.0.1:8100";

export default defineConfig({
  base: "/pioneer/",
  plugins: [react(), tailwindcss()],
  // чанк страницы скриптов — это Monaco (~2.7 МБ, ~700 КБ gzip), грузится лениво
  build: { chunkSizeWarningLimit: 4500 },
  server: {
    port: Number(process.env.PIONEER_UI_PORT ?? 5173),
    strictPort: true,
    proxy: {
      "/pioneer/api": `http://${core}`,
      "/pioneer/ws": { target: `ws://${core}`, ws: true },
    },
  },
});
