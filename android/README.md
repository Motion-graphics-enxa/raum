# RAUM for Android (F-09)

A small WebView app around the box's own web app. No second UI: the WebView shows
`https://<box>[:port]/…` exactly as Chrome would. The only native screen is the box address.
Java, platform APIs only (no AndroidX, no Play Services, no runtime dependencies), minSdk 26.

## Why a plain WebView app (not a TWA, not Capacitor)

| | Trusted Web Activity (Bubblewrap) | Capacitor | **Plain WebView (this)** |
|---|---|---|---|
| Box on a LAN IP with a local-CA certificate | Needs Digital Asset Links at `https://<origin>/.well-known/assetlinks.json`, verified for one origin fixed at build time. A LAN IP / local CA cannot be verified, so Chrome falls back to a Custom Tab with a URL bar. | Works, but `server.url` (remote content) is a dev-only path; the user CA still needs the same network-security config, and the mic grant still needs native code | Works: `network_security_config` trusts user-installed CAs for the WebView and the health check |
| Box address chosen by the user, changeable | No (origin baked into the APK) | Rebuild or plugin | Yes: asked once, checked against `/api/health`, saved |
| Mic for the box origin only | Chrome's own per-site prompt | Plugin or native code anyway | `onPermissionRequest`: audio, box origin only, after the OS permission |
| Keep the screen on in a conversation | Web Wake Lock only | Plugin | `FLAG_KEEP_SCREEN_ON` on talk / table / listen pages |
| Toolchain and size | Node + Bubblewrap CLI | Node + Capacitor + AndroidX | Gradle + the platform SDK; APK of a few dozen KB of code |

A TWA would fit a box with a public origin (e.g. a fixed Tailscale name); RAUM boxes are reached by
LAN IP (plan §6.14), so the WebView shell is the one that works everywhere.

## What it does

| | |
|---|---|
| Box address | Asked once (or taken from a QR link, after a tap), checked against `GET /api/health` (`service: raum-core`), stored in the app's private preferences. Change it: long-press the launcher icon › **Box address** (or open `raum://setup`). |
| Microphone | `RECORD_AUDIO` (asked when the web app first asks), then granted to the box origin only; every other permission request (camera, location, MIDI, …) is denied. `MODIFY_AUDIO_SETTINGS` for WebRTC audio routing. |
| Screen | Kept on while the WebView shows `/r/…` (talk, summary), `/t/…` (table screen) or `/l/…` (listen); normal otherwise. |
| Rotation, dark mode | Handled in place (`configChanges`), so the conversation's WebView and its LiveKit connection survive. |
| Navigation | Only the box origin loads in the WebView. Links elsewhere open in the browser, and only when tapped. |
| Document camera (F-07) | `<input capture>` opens the system camera app (no `CAMERA` permission); the photo goes to a private cache file (`ShotProvider`), from there to the box, and is deleted on the next shot and when the app closes. |
| Print / PDF | The summary's print button goes to Android printing (save as PDF). |
| Privacy | No Safe Browsing lookups, no WebView metrics, no backup of app data, no analytics. Cleartext HTTP is off. |
| Debugging | Debug builds allow `chrome://inspect` on a desktop Chrome over USB. |

## Links and QR codes

- `raum://j/<room>?role=a|b&lang=xx`, `raum://join?room=<room>&role=..`, `raum://l/<room>`,
  `raum://t/<room>?theme=dark`, `raum://open`, `raum://setup` always open the app (from a browser
  page, a message, NFC). They open on the configured box; unknown routes/parameters are dropped.
- `https://<box>/j/<room>?role=..` (the room QR codes) carry their box. If it is not the box this phone
  uses, the app shows it and asks for a tap on **Connect**; a link never switches boxes on its own.
  Whether Android hands these https links to the app at all depends on the version:
  - Android 11 and older: a chooser offers RAUM (the default APK matches any host, `/j/` paths only).
  - Android 12 and newer: unverified web links always open the browser, unless the app is approved
    for that host. Build the APK for the box's address (`link_host` input of the workflow, or
    `-PraumLinkHost=192.168.1.50`), then on each phone: Settings › Apps › RAUM › Open by default ›
    Add link › tick the box. (Digital Asset Links verification is impossible for a LAN IP.)
  - Without that, the phone camera opens the QR in the browser. See the recommended web-app change in
    the report: an "Open in the RAUM app" link (`raum://j/<room>?role=..`) on the join page.

## Get the APK (CI)

`.github/workflows/android.yml`: **Actions › android › Run workflow** (input `link_host`, default `*`)
or a `v*` tag. Runs the unit tests (`BoxAddressTest`) and `assembleDebug`; the artifact
`raum-android-debug-apk` holds `raum-<ref>-links-<host>-debug.apk`, signed with the runner's throwaway
debug key (nothing secret in the repo). A later debug build from another runner has a different key:
uninstall the old app first.

```bash
gh workflow run android.yml -f link_host=192.168.1.50
gh run download --name raum-android-debug-apk
adb install -r raum-*-debug.apk          # or copy it to the phone and allow "Install unknown apps"
```

## Trust the box's certificate (once per phone)

The box serves HTTPS with its own local CA (plan §9.3 B). Get `raum-root.crt` as in
`deploy/Caddyfile` / `desktop/README.md`, copy it to the phone, then:

- Android 11 and newer: Settings › Security (& privacy) › More security settings › Encryption &
  credentials › Install a certificate › **CA certificate** › Install anyway › pick `raum-root.crt`.
  (Tapping the file in a browser or Files app does not install CA certificates on 11+.)
- Android 8–10: Settings › Security › Encryption & credentials › Install from storage (or open the
  file), choose "VPN and apps".
- Samsung: Settings › Biometrics and security › Other security settings › Install from device storage
  › CA certificate.

The app opts in to user-installed CAs (`res/xml/network_security_config.xml`); Chrome on the same phone
trusts it too. Remove it under Encryption & credentials › User credentials / Trusted credentials › User.
Without a CA install: the Tailscale certificate (plan §9.3 A) is publicly trusted.

## Build locally

Android Studio (open `android/`), or Gradle 8.11.1 with JDK 17 and the Android SDK (platform 35,
build-tools 35.0.0). No Gradle wrapper is checked in; CI installs Gradle itself.

```bash
cd android
gradle testDebugUnitTest assembleDebug            # app/build/outputs/apk/debug/app-debug.apk
gradle assembleDebug -PraumLinkHost=192.168.1.50  # QR links for one box (Android 12+)
```

`applicationId` is `de.raum.android` (a placeholder until the project has a domain). Release signing
is not set up; a keystore belongs in CI secrets, never in the repo.
