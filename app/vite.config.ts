import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// RAUM dev server.
//  - /api (+ WebSocket /api/events) -> raum-core :8000
//  - /rtc (LiveKit signalling)      -> livekit-server :7880
// So the app, the API and LiveKit share one origin, like Caddy does on the box.
// RAUM_HTTPS=1 serves a self-signed cert so phones on the LAN may use the microphone.
const https = process.env.RAUM_HTTPS === "1";
const core = process.env.RAUM_CORE ?? "http://127.0.0.1:8000";
const livekit = process.env.RAUM_LIVEKIT ?? "http://127.0.0.1:7880";
const port = Number(process.env.RAUM_APP_PORT ?? 5180);

export default defineConfig({
  plugins: [react(), ...(https ? [basicSsl()] : [])],
  build: { chunkSizeWarningLimit: 900 }, // livekit-client is one large chunk
  server: {
    host: true,
    port,
    strictPort: true,
    proxy: {
      "/api": { target: core, changeOrigin: true, ws: true, xfwd: true },
      "/rtc": { target: livekit, changeOrigin: true, ws: true },
    },
  },
  preview: {
    host: true,
    port: 4173,
    proxy: {
      "/api": { target: core, changeOrigin: true, ws: true, xfwd: true },
      "/rtc": { target: livekit, changeOrigin: true, ws: true },
    },
  },
});
