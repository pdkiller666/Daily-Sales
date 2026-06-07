/* DailySales Service Worker v5 */
const CACHE_NAME = 'dailysales-v5';
const STATIC_ASSETS = [
    '/static/logo.jpg',
    '/static/icon.svg',
    '/static/icon-maskable.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/apple-touch-icon.png',
    '/static/manifest.json',
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
    /* All authenticated routes: network-only (never cache sensitive data) */
});

/* ── Push notifications ── */
self.addEventListener('push', e => {
    if (!e.data) return;
    let data;
    try { data = e.data.json(); } catch { data = { title: 'DailySales', body: e.data.text() }; }

    const showNotif = self.registration.showNotification(data.title || 'DailySales', {
        body: data.body || '',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        tag: data.tag || 'dailysales',
        renotify: true,
        data: { url: data.url || '/dashboard' },
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

    e.waitUntil(Promise.all([showNotif, updateBadge]));
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
    /* Re-subscribe automatically and send new subscription to server */
    if (!e.oldSubscription) return;   /* guard: old sub may be null */
    e.waitUntil(
        self.registration.pushManager.subscribe(e.oldSubscription.options)
            .then(sub => fetch('/api/push/subscribe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(sub.toJSON()),
                credentials: 'same-origin',
            }))
            .catch(() => {})
    );
});
