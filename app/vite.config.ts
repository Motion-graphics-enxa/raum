import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

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

// Long-lived WebSockets (/api/events, /rtc) have no idle timeout here: http-proxy sets
// setTimeout(0) + TCP keep-alive on both sockets, and Node's request timeouts end at the upgrade.
// Core ticks /api/events every second and answers the app's "ping". A phone that sleeps or changes
// network resets its socket (vite: "ws proxy socket error: read ECONNRESET"); the app reconnects with
// ?since= (src/lib/link.ts). Log which socket it was: /api/events (text) or /rtc (LiveKit signalling).
type WsProxy = { on(ev: "proxyReqWs", fn: (proxyReq: unknown, req: { url?: string }, socket: NodeSocket) => void): void };
type NodeSocket = { on(ev: "error", fn: (err: Error) => void): void; remoteAddress?: string };
const logResets = (proxy: unknown) =>
  (proxy as WsProxy).on("proxyReqWs", (_proxyReq, req, socket) => {
    socket.on("error", (err) => {
      console.warn(`[raum] ws ${req.url?.split("?")[0] ?? "?"} from ${socket.remoteAddress ?? "?"}: ${err.message}`);
    });
  });
const proxy = {
  "/api": { target: core, changeOrigin: true, ws: true, xfwd: true, configure: logResets },
  "/rtc": { target: livekit, changeOrigin: true, ws: true, configure: logResets },
};

// `vite build --mode site` builds the public website instead: only the landing page, from site/index.html into
// dist-site/, which Vercel serves (vercel.json, api/waitlist.ts). The app build above is unchanged.
const site = {
  root: fileURLToPath(new URL("./site", import.meta.url)),
  publicDir: fileURLToPath(new URL("./public", import.meta.url)),
  build: { outDir: fileURLToPath(new URL("./dist-site", import.meta.url)), emptyOutDir: true },
};

export default defineConfig(({ mode }) => ({
  plugins: [react(), ...(https ? [basicSsl()] : [])],
  build: { chunkSizeWarningLimit: 900 }, // livekit-client is one large chunk
  server: {
    https: tls,
    host: true,
    port,
    strictPort: true,
    proxy,
  },
  // deploy/box.sh serves the built app with `vite preview`: the same proxy, the same WebSockets
  preview: {
    https: tls,
    host: true,
    port: 4173,
    proxy,
  },
  ...(mode === "site" ? site : {}),
}));
