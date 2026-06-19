---
name: APK download from private GitHub repo
description: Why server-side APK download from a private GitHub repo fails on Amvera, and the robust webhook-push fix
---

# APK distribution: private GitHub repo → Amvera

## Root cause of the long-standing 404
The server-side APK download/caching feature tried to fetch the release asset
from GitHub **at runtime**. The repo is **private**, so that requires a
`GITHUB_TOKEN` in the *running app's* environment. On Amvera that token was
**never set** — `GITHUB_TOKEN` historically existed only as a Replit/deploy
secret used by `deploy.sh` for `git push` (see AGENT_HANDOFF: "токен для push
на GitHub"). So the server could not authenticate → `HTTP 404` on every
download (startup check, webhook fallback, route).
**Why:** the local-caching feature was the first code path that needed the app
itself to talk to GitHub; it was built assuming a token that only ever lived in
the deploy environment. It worked in Replit (token present), failed on Amvera.
Two extra red herrings: a private-repo `browser_download_url` returns 404 even
*with* a token (must use the asset API url + `Accept: application/octet-stream`),
and `_APK_MIN_SIZE` was once 1 MB which silently deleted the valid ~544 KB APK.

## The robust fix (current architecture)
**Do NOT make the server fetch from GitHub.** Instead, GitHub Actions
(`build-twa.yml`) — which already has the built `.apk` in hand — POSTs the
**binary** straight to the server webhook `POST /webhook/apk-binary`
(`--data-binary @file`, `Content-Type: application/octet-stream`, auth via
`Authorization: Bearer $APK_WEBHOOK_SECRET`, version in `X-APK-Version`). The
handler validates the secret + min size and writes the bytes to the persistent
volume (`data/apk/DailySales-latest.apk` via tmp+replace). `/download/android`
then serves that local file. **Amvera never calls GitHub → token problem gone
forever.** `APK_WEBHOOK_SECRET` is already configured on both sides (the older
JSON `/webhook/apk-release` used it).

## Gotcha: deploy-timing race (503)
`deploy.sh` pushes `web/app.py` (→ Amvera restart) and `.github/workflows/*`
(→ triggers a build) in the **same** push. The build finishes and POSTs the APK
*while Amvera is still restarting* → upload step gets **HTTP 503**. Fix: after
the deploy settles, re-run the build (`workflow_dispatch`) so the upload hits a
live server. Verify success via the step log (`APK binary upload HTTP status:
200`, `{"ok":true,"bytes":...}`) and `curl /download/android`
(expect `application/vnd.android.package-archive`, first bytes `PK\x03\x04`).

## How to apply
- Never redirect end-users to a GitHub URL for the APK — serve the local file;
  show an auto-refresh "preparing" page only if it isn't cached yet.
- Keep `_APK_MIN_SIZE` below the real APK size (~544 KB); ~200 KB is safe.
- The legacy `_download_apk_to_local` (resolves browser_download_url → asset API
  url) still exists as a fallback but is moot once the webhook push works.
