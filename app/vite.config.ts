import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";
import { readFileSync } from "node:fs";

// RAUM dev server.
//  - /api (+ WebSocket /api/events) -> raum-core :8000
//  - /rtc (LiveKit signalling)      -> livekit-server :7880
// So the app, the API and LiveKit share one origin, like Caddy does on the box.
// RAUM_HTTPS=1 serves a self-signed cert so phones on the LAN may use the microphone.
// RAUM_TLS_CERT + RAUM_TLS_KEY (PEM paths) serve our own certificate instead, e.g. one signed by a
// local CA that team phones trust (plan §9.3 B). They win over the self-signed RAUM_HTTPS=1 cert.
const tlsCert = process.env.RAUM_TLS_CERT;
const tlsKey = process.env.RAUM_TLS_KEY;
const tls = tlsCert && tlsKey ? { cert: readFileSync(tlsCert), key: readFileSync(tlsKey) } : undefined;
const https = process.env.RAUM_HTTPS === "1" && !tls;
const core = process.env.RAUM_CORE ?? "http://127.0.0.1:8000";
const livekit = process.env.RAUM_LIVEKIT ?? "http://127.0.0.1:7880";
const port = Number(process.env.RAUM_APP_PORT ?? 5180);

export default defineConfig({
  plugins: [react(), ...(https ? [basicSsl()] : [])],
  build: { chunkSizeWarningLimit: 900 }, // livekit-client is one large chunk
  server: {
    https: tls,
    host: true,
    port,
    strictPort: true,
    proxy: {
      "/api": { target: core, changeOrigin: true, ws: true, xfwd: true },
      "/rtc": { target: livekit, changeOrigin: true, ws: true },
    },
  },
  preview: {
    https: tls,
    host: true,
    port: 4173,
    proxy: {
      "/api": { target: core, changeOrigin: true, ws: true, xfwd: true },
      "/rtc": { target: livekit, changeOrigin: true, ws: true },
    },
  },
});
