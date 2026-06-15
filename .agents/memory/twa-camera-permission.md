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

## NEVER gate the camera flow on navigator.permissions.query in a TWA

**Rule:** Do **not** add a pre-flight `navigator.permissions.query({name:'camera'})` that
early-returns/shows an error when state is `denied`. In the TWA wrapper this query returns `denied`
**falsely** — even when there is NO site-level camera block (Chrome → Site settings → the origin
shows only Notifications/Sound, no Camera entry) and the global "sites can ask" is on. Gating on it
means `getUserMedia` is never called, so Chrome's in-page camera prompt never appears and the user
can never grant — a self-inflicted dead end.

**Why:** In a TWA, camera is **not** delegated to the Android app the way notifications/geolocation
are. `getUserMedia` triggers Chrome's own in-page permission prompt; that call is the only reliable
source of truth. The permissions API is unreliable in the TWA context. The Android app's CAMERA
manifest permission is still required (for hardware access), but it is necessary, not sufficient —
the actual grant happens through Chrome's getUserMedia prompt.

**How to apply:** the scanner in both templates (`web/templates/pos/index.html`,
`web/templates/sales/index.html` — duplicated, fix both) must call `getUserMedia` directly and only
show an error in the `catch` (NotAllowedError → context-aware copy). Manifest CAMERA permission in
`android/app/src/main/AndroidManifest.xml`. If a user ever DID tap "Block" in Chrome's prompt, the
fix is Chrome → ⋮ → Settings → Site settings → All sites → origin → "Clear & reset" (forces a fresh
prompt); but do not assume denial in code — let getUserMedia prompt every time.

## Re-entry guard (run token)

`openBarcodeScanner()` is async and can be invoked concurrently (rapid retry/open taps,
or open while a previous open is still awaiting getUserMedia). Without a guard this
double-starts detectors and leaks the previous camera stream.

**Rule:** bump a monotonic `this._scanRunId` at the top of open; capture it locally; after
every `await` (getUserMedia, video.play) bail if `runId !== this._scanRunId` (and stop the
stream you just got). `closeBarcodeScanner()` also bumps the token to cancel any in-flight
open. With this, `restartScan()` is just `openBarcodeScanner()` — it self-cleans on entry
(bumps token, cancels RAF, resets zxing reader, stops old stream).
