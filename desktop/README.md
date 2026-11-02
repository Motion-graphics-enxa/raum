# RAUM desktop (F-08)

A native window around the box's own web app, for Mac, Windows and Linux laptops. Tauri v2. There is
no second UI: the window shows `https://<box>[:port]/…` exactly as a browser would. The only local
page is the box address (`setup/`).

```
first run          setup/index.html: box address  ──GET /api/health──▶ "service": "raum-core"?
                                       │ yes: saved to box.json
every later start  connects on its own ┴──▶ window shows https://<box>/  (or the raum:// room)
```

| | |
|---|---|
| Box address | Asked once, checked against `GET /api/health` (must answer `service: raum-core`), saved as `box.json` in the app config dir (macOS `~/Library/Application Support/de.raum.desktop/`, Windows `%APPDATA%\de.raum.desktop\`, Linux `~/.config/de.raum.desktop/`). Change it: menu **Box › Box address…** (`Cmd/Ctrl+Shift+B`). Delete the file to forget the box. |
| Microphone | Granted to pages of the box origin only, every other request is denied (`on_permission_request`). macOS asks once (`NSMicrophoneUsageDescription` in `src-tauri/Info.plist`; hardened-runtime entitlement `com.apple.security.device.audio-input`). |
| Navigation | A window shows the setup page or the box origin, nothing else. `target=_blank` links to the box (table screen, a second seat) open a second window; other sites open nowhere. |
| IPC | Two commands (`setup_state`, `connect`), callable from the local setup page only (`src-tauri/capabilities/setup.json`). Box pages get no IPC; no fs/shell/http plugins. |
| TLS | The operating system's trust store, for the health check (rustls-platform-verifier) and the webview alike. Verification is never switched off: an untrusted box certificate is reported as such. Trust the box's CA once per computer (below). |
| Links | `raum://j/<room>?role=a\|b&lang=xx`, `raum://join?room=<room>&role=..`, `raum://l/<room>`, `raum://t/<room>?theme=dark`, `raum://open`. Unknown routes and parameters are dropped; a link can never change which box the app talks to. |
| Print / PDF | The summary's print button: WebView2 and WebKitGTK print natively; on macOS a small shim hands `window.print()` to the native print panel. |

## Get the bundles (CI)

`.github/workflows/desktop.yml` builds on **Actions › desktop › Run workflow** (option: universal macOS
build) or on a `v*` tag; nothing runs on push or PR. Artifacts (14 days):

| Artifact | Contents | Built on |
|---|---|---|
| `raum-desktop-macos` | `RAUM_<v>_aarch64.dmg` (or `_universal`) | macos-latest |
| `raum-desktop-windows` | `RAUM_<v>_x64-setup.exe` (NSIS, per-user), `RAUM_<v>_x64_en-US.msi` | windows-latest |
| `raum-desktop-linux` | `raum_<v>_amd64.deb`, `RAUM_<v>_amd64.AppImage` | ubuntu-24.04 (glibc 2.39) |

```bash
gh workflow run desktop.yml                       # or: -f macos_universal=true
gh run download --name raum-desktop-macos         # when it is done
```

The bundles are **not signed** (no certificates in this repo):
- macOS: ad-hoc signed only. After copying `RAUM.app` to Applications: `xattr -dr com.apple.quarantine /Applications/RAUM.app` (or right-click › Open once). For distribution, set `APPLE_SIGNING_IDENTITY` / notarization secrets and drop `"signingIdentity": "-"` from `tauri.conf.json`.
- Windows: SmartScreen › More info › Run anyway. WebView2 is part of Windows 10/11; the installer fetches it otherwise.
- Linux: `sudo apt install ./raum_*.deb` (pulls the GStreamer plugins WebKitGTK needs for the microphone and WebRTC), or `chmod +x RAUM_*.AppImage` (bundles GStreamer).

## Trust the box's certificate

The box serves HTTPS with its own local CA (Caddy `tls internal`, plan §9.3 B). Install that CA's
**root** certificate once per computer; the app (and every browser on it) then trusts the box. Get it
from the box (as in `deploy/Caddyfile`):

```bash
docker compose -f /opt/raum/app/deploy/docker-compose.yml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt ./raum-root.crt
```

(With `scripts/dev.sh --https` and `RAUM_TLS_CERT`, use the CA that signed that certificate, e.g.
`mkcert -CAROOT`/rootCA.pem. Vite's `RAUM_HTTPS=1` self-signed certificate has no CA; the app will
refuse it, by design.) Check the fingerprint against the box before trusting it:
`openssl x509 -in raum-root.crt -noout -subject -fingerprint -sha256`.

| OS | Install the root (then restart RAUM) |
|---|---|
| macOS | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain raum-root.crt` — or Keychain Access › System › File › Import, then the certificate › Trust › *Always Trust*. |
| Windows | Admin PowerShell: `certutil -addstore -f Root raum-root.crt` (all users), or without admin: `certutil -user -addstore Root raum-root.crt` and confirm the dialog. GUI: double-click › Install Certificate › *Trusted Root Certification Authorities*. |
| Debian / Ubuntu | `sudo cp raum-root.crt /usr/local/share/ca-certificates/raum-root.crt && sudo update-ca-certificates` |
| Fedora / RHEL | `sudo cp raum-root.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust` |
| Arch | `sudo trust anchor --store raum-root.crt` |

Remove it again: macOS `sudo security delete-certificate -c "Caddy Local Authority" /Library/Keychains/System.keychain`
(check the name with the `-subject` command above); Windows `certutil -delstore Root "<subject CN>"`;
Linux delete the file and rerun `update-ca-certificates` / `update-ca-trust`.

No CA to install at all: serve the box under its Tailscale certificate (plan §9.3 A, publicly trusted)
and enter that name (`https://<box>.<tailnet>.ts.net`) as the box address.

## Build locally

Needs Rust (stable) and Node; Linux also `libwebkit2gtk-4.1-dev librsvg2-dev libxdo-dev build-essential`.

```bash
cd desktop
npm ci                                    # the Tauri CLI only (pinned in package.json)
npx tauri dev                             # run it
npx tauri build --bundles app             # macOS .app; see the CI workflow for the others
cd src-tauri && cargo test --lib          # address rules, deep links, health check
RAUM_TEST_BOX=https://192.168.1.50:5180 cargo test --lib -- --ignored live_box   # against a box
cargo clean                               # target/ grows to several GB
```

Icons come from `app/public/favicon.svg`: `npm run icons` renders every size into `icon/out/` (via
`icon/manifest.json`; `icon/mark.svg` is the favicon's mark padded to the Android safe zone). Copy
`32x32.png 128x128.png 128x128@2x.png icon.icns icon.ico icon.png` to `src-tauri/icons/` and
`android/mipmap-*` to `android/app/src/main/res/`.

## Known limits

- Linux: the microphone and WebRTC need a WebKitGTK built with GStreamer WebRTC (Ubuntu 24.04+,
  Fedora 40+). The app switches `enable-media-stream` and `enable-webrtc` on; if a distro's build lacks
  WebRTC, the room will not connect: use Chromium or Firefox there.
- macOS 12 or later (WKWebView media-capture permission API).
- The macOS microphone grant is tied to the code signature; ad-hoc builds may ask again after an update.
