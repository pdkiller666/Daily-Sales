/* DailySales Service Worker v2 */
const CACHE_NAME = 'dailysales-v2';
const STATIC_ASSETS = [
    '/static/logo.jpg',
    '/static/icon.svg',
    '/static/icon-maskable.svg',
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

    /* Cache-first for static assets */
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

/* ── Push notifications (future VAPID) ── */
self.addEventListener('push', e => {
    if (!e.data) return;
    let data;
    try { data = e.data.json(); } catch { data = { title: 'DailySales', body: e.data.text() }; }
    e.waitUntil(
        self.registration.showNotification(data.title || 'DailySales', {
            body: data.body || '',
            icon: '/static/icon.svg',
            badge: '/static/icon.svg',
            tag: data.tag || 'dailysales',
            renotify: true,
            data: { url: data.url || '/dashboard' },
        })
    );
});

self.addEventListener('notificationclick', e => {
    e.notification.close();
    const target = (e.notification.data && e.notification.data.url) || '/dashboard';
    e.waitUntil(clients.openWindow(target));
});
