---
name: TWA / web-app open buttons
description: How the bot/web "open the app" choice works — intent:// indirection, TWA cookie sharing, and the XSS pitfall when reflecting login codes into inline JS
---

# Open-app choice: Mini App vs Android APK vs browser

Bot "🌐 Веб-интерфейс" menu offers three paths; web banner offers open-or-download.

## Why an intermediate `/open-app` page exists
- Telegram inline button `url=` accepts only http/https/tg — **NOT `intent://`**. To open an installed Android app from a Telegram button you must point the button at an HTTPS page that JS-redirects to the intent URL.
- Intent URL shape: `intent://HOST/PATH#Intent;scheme=https;package=com.dailysales.app;S.browser_fallback_url=<url-encoded>;end`. App installed → opens TWA; not installed → browser navigates to `browser_fallback_url` (we point it at `/download/android` to offer install). Only one branch runs.

## TWA shares the Chrome cookie jar
- A TWA opened via intent runs on top of Chrome and **shares cookies with the Chrome browser**. So the web "Открыть в приложении" button just intents to `/dashboard` with no login code — the existing session carries over automatically.
- From the **bot**, no browser session exists yet, so `/open-app` deep-links to `/auth/code/auto?c=CODE` (one-time 5-min code) for auto-login inside the app.

## Single-use code reuse
- One `generate_code()` code is reused for BOTH the browser magic link and `/open-app` (user clicks only one). `generate_code` revokes the user's previous code, so do NOT generate two codes in the same handler — the second invalidates the first.

## XSS pitfall (was a real finding)
- **`json.dumps(value)` does NOT neutralize `</script>`** — reflecting it into an inline `<script>` is reflected XSS (`?c=</script><script>...`). Login codes are always digits, so sanitize server-side: `re.sub(r"\D","",c)[:16]`, and additionally `.replace("</","<\\/")` for defense-in-depth. Never trust json.dumps alone to make a value safe inside `<script>`.

## getInstalledRelatedApps detection
- Web banner uses `navigator.getInstalledRelatedApps()` (needs `related_applications`+`prefer_related_applications:false` in manifest.json). Unreliable for sideloaded/GitHub-release TWAs — may miss installed users. Acceptable: falls back to showing "Скачать"; intent:// still works if the user taps through.
