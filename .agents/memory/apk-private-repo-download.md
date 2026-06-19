---
name: APK download from private GitHub repo
description: Why APK download breaks when the GitHub repo is private, and the exact server-side fix
---

# APK download from a PRIVATE GitHub repo

## The rule
For a **private** GitHub repository, a release asset's `browser_download_url`
(`github.com/OWNER/REPO/releases/download/TAG/NAME`) returns **HTTP 404 even
with a valid `Authorization: Bearer <token>` header**. It only works for
public repos or in a browser session with cookies.

To download a private-repo asset programmatically you MUST:
1. Use the asset's **API URL** (`api.github.com/repos/OWNER/REPO/releases/assets/<ID>`)
2. Send header `Accept: application/octet-stream`
3. Send `Authorization: Bearer <token>`

The asset ID is not in the browser_download_url — resolve it by querying
`api.github.com/repos/OWNER/REPO/releases/tags/<TAG>` (or `.../releases/latest`)
and matching `asset.name`, then use `asset.url` (the API url).

## Why this bit us
The APK download worked for a long time, then silently broke. **Root cause:
the repo was switched from public to private.** While public, the
browser_download_url worked directly. After going private, every download
(startup check, webhook, fire-and-forget) got 404. Symptom in Amvera logs:
`ERROR:root:APK download HTTP 404: https://github.com/.../releases/download/apk-NN/...apk`

## How to apply
- Server-side downloader (`_download_apk_to_local` in `web/app.py`) resolves
  any github.com browser_download_url → API asset url, then downloads with
  `Accept: application/octet-stream`. Centralizing here fixes all callers
  (startup, webhook payload `apk_url`, route fallback) without touching the
  GitHub Actions workflow (which still sends browser_download_url).
- `_APK_MIN_SIZE` must stay below the real APK size (~555 KB) — it was once
  set to 1 MB which silently deleted every valid download. Keep ~200 KB.
- The repo intentionally stays private; users download the APK from the
  Amvera domain (`/download/android` serves a local FileResponse). Never
  redirect end-users to a GitHub URL — when the local file isn't cached yet,
  show an auto-refresh "preparing" HTML page instead.
