---
name: TWA camera permission
description: Why getUserMedia fails inside the Android TWA APK and how the scan retry must work
---

# TWA / standalone camera access

**Rule:** The Android TWA APK must declare `<uses-permission android:name="android.permission.CAMERA" />`
(plus `<uses-feature ... required="false">`) in `android/app/src/main/AndroidManifest.xml`.
Without it `getUserMedia` rejects **instantly** with `NotAllowedError` inside the installed app
(symptom: scanner modal flickers, then "доступ запрещён", with no OS permission prompt).
Browser/Chrome tab users are unaffected — only the TWA wrapper needs the manifest permission.

**Why:** A TWA runs web content through Chrome, but the OS camera permission is owned by the
*Android app*. If the app's manifest doesn't request CAMERA, Chrome can never obtain it, so every
getUserMedia call fails fast. Manifest change only takes effect after a new APK is built (GitHub
Actions Gradle build) and reinstalled.

**Scan retry (`restartScan`) must fully re-init**, not just restart the detector: stop old tracks,
null the video.srcObject, then call `openBarcodeScanner()` again (which re-runs getUserMedia).
The old version only restarted the detector and guarded on `this._scanStream` — after a denied
first attempt the stream was null, so retry showed a **black screen** and did nothing.

**Error copy is context-aware:** in standalone mode (`matchMedia('(display-mode: standalone)')`)
there is no address bar, so the "tap 🔒 near the address bar" hint is replaced with
"Settings → Apps → DailySales → Permissions → Camera".

**How to apply:** lives in `web/templates/pos/index.html` and `web/templates/sales/index.html`
(duplicated scanner logic — fix both). Manifest in `android/app/src/main/AndroidManifest.xml`.
