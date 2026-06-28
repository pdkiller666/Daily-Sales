/* DailySales Service Worker v11 */
const CACHE_NAME = 'dailysales-v11';
const OFFLINE_URL = '/static/offline.html';
const STATIC_ASSETS = [
    '/static/logo.jpg',
    '/static/icon.svg',
    '/static/icon-maskable.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/apple-touch-icon.png',
    '/static/manifest.json',
    OFFLINE_URL,
];

/* ── Install: pre-cache static assets ── */
self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

/* ── Activate: remove old caches ── */
self.addEventListener('activate', e => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

/* ── Fetch strategy ── */
self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);

    /* Cache-first for /static/ assets */
    if (url.pathname.startsWith('/static/')) {
        e.respondWith(
            caches.match(e.request).then(cached => {
                if (cached) return cached;
                return fetch(e.request).then(res => {
                    if (res && res.status === 200) {
                        const clone = res.clone();
                        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
                    }
                    return res;
                });
            })
        );
        return;
    }

    /* Navigations: network-first, fall back to branded offline page when the
       network is unreachable. We never cache the actual page HTML (sensitive
       per-tenant data) — only serve the static offline shell on failure. */
    if (e.request.mode === 'navigate') {
        e.respondWith(
            fetch(e.request).catch(() =>
                caches.match(OFFLINE_URL).then(r => r || new Response(
                    '<h1>Нет соединения</h1>', { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
                ))
            )
        );
        return;
    }
    /* All other authenticated requests: network-only (never cache sensitive data) */
});

/* ── Push notifications ── */
self.addEventListener('push', e => {
    if (!e.data) return;
    let data;
    try { data = e.data.json(); } catch { data = { title: 'DailySales', body: e.data.text() }; }

    const tag = data.tag || 'ds-default';
    const showNotif = self.registration.showNotification(data.title || 'DailySales', {
        body: data.body || data.title || 'DailySales',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        tag: tag,
        /* renotify only for real-time events (sales, chat), not for reports/alerts */
        renotify: tag === 'ds-sale' || tag === 'ds-chat',
        data: { url: data.url || '/dashboard' },
        silent: false,
    });

    /* Fetch real unread count for accurate App Badge */
    const updateBadge = fetch('/api/unread-count', { credentials: 'same-origin' })
        .then(r => r.json())
        .then(d => {
            const total = (d && typeof d.total === 'number') ? d.total : 1;
            if ('setAppBadge' in self.navigator) self.navigator.setAppBadge(total).catch(() => {});
        })
        .catch(() => {
            /* Fallback: set badge to 1 if fetch fails (app closed / offline) */
            if ('setAppBadge' in self.navigator) self.navigator.setAppBadge(1).catch(() => {});
        });

    /* Notify open clients when a test push (tag=ds-sub) is received so
       the diagnostics card can refresh without waiting for the user to act. */
    const notifyClients = (tag === 'ds-sub')
        ? clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then(wins => wins.forEach(w => w.postMessage({ type: 'ds-push-received', tag })))
        : Promise.resolve();

    e.waitUntil(Promise.all([showNotif, updateBadge, notifyClients]));
});

self.addEventListener('notificationclick', e => {
    e.notification.close();
    /* Clear badge when user taps notification */
    if ('clearAppBadge' in self.navigator) {
        self.navigator.clearAppBadge().catch(() => {});
    }
    const target = (e.notification.data && e.notification.data.url) || '/dashboard';
    e.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(wins => {
            const existing = wins.find(w => w.url.includes(target));
            if (existing) return existing.focus();
            return clients.openWindow(target);
        })
    );
});

/* ── Push subscription change ── */
self.addEventListener('pushsubscriptionchange', e => {
    /* Re-subscribe automatically and send new subscription to server.
       NOTE: e.oldSubscription can be null on Chrome/Android — the old guard
       `if (!e.oldSubscription) return` was a bug that silently abandoned renewal.
       Strategy (most → least capable):
         1. e.newSubscription  — some browsers provide the renewed sub directly
         2. getSubscription()  — existing sub still valid, just re-sync to server
         3. subscribe(opts)    — expired; re-create using oldSubscription.options  */
    e.waitUntil((async () => {
        try {
            /* 1. Browser already renewed it */
            if (e.newSubscription) {
                await fetch('/api/push/subscribe', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(e.newSubscription.toJSON()),
                    credentials: 'same-origin',
                });
                return;
            }
            /* 2. Current subscription may still be alive */
            let sub = await self.registration.pushManager.getSubscription();
            if (!sub) {
                /* 3. Expired — re-subscribe using old options (carries applicationServerKey) */
                const opts = e.oldSubscription && e.oldSubscription.options;
                if (!opts) return; /* no options → can't re-subscribe without server key */
                sub = await self.registration.pushManager.subscribe(opts);
            }
            await fetch('/api/push/subscribe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(sub.toJSON()),
                credentials: 'same-origin',
            });
        } catch (_) { /* best-effort */ }
    })());
});
