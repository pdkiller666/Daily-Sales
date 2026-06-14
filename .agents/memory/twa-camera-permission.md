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
there is no address bar, so the "tap 🔒 near the address bar" hint is replaced with TWA guidance.

## Chrome stores the camera denial per-origin, NOT in the Android app

**Rule:** Once the user taps "Block" in Chrome's camera prompt, Chrome remembers the denial for
that *origin* — independently of the Android app's CAMERA permission. Granting the Android app
permission AND reinstalling the TWA APK does **not** clear it, because Chrome is a separate app
with its own per-site data. `navigator.permissions.query({name:'camera'})` then returns `denied`
and `getUserMedia` rejects instantly.

**Why:** TWA web content runs in Chrome; Chrome owns the web-origin permission, the Android OS owns
the app permission. Both must allow camera. The Android grant is necessary but not sufficient.

**The only reliable user fix** (the per-site "Camera" list often doesn't show the origin):
Chrome app → ⋮ → Settings → Site settings → **All sites** → find the origin → **"Clear & reset"**.
This wipes the remembered denial AND the service-worker cache, so the next getUserMedia prompts fresh
and the new template loads. We do a pre-flight `navigator.permissions.query({name:'camera'})` and, if
`denied`, show this Clear&Reset instruction instead of attempting getUserMedia.

**How to apply:** pre-flight check + denied-state copy live in both scanner templates
(`web/templates/pos/index.html`, `web/templates/sales/index.html` — duplicated scanner logic, fix
both). Manifest CAMERA permission in `android/app/src/main/AndroidManifest.xml`.

## Re-entry guard (run token)

`openBarcodeScanner()` is async and can be invoked concurrently (rapid retry/open taps,
or open while a previous open is still awaiting getUserMedia). Without a guard this
double-starts detectors and leaks the previous camera stream.

**Rule:** bump a monotonic `this._scanRunId` at the top of open; capture it locally; after
every `await` (getUserMedia, video.play) bail if `runId !== this._scanRunId` (and stop the
stream you just got). `closeBarcodeScanner()` also bumps the token to cancel any in-flight
open. With this, `restartScan()` is just `openBarcodeScanner()` — it self-cleans on entry
(bumps token, cancels RAF, resets zxing reader, stops old stream).
