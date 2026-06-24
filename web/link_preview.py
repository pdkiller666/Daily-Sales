"""OpenGraph link previews для веб-чата.

Безопасный серверный фетч метаданных страницы (title/description/image/site_name)
по первой http(s)-ссылке в сообщении. Результат кэшируется в отдельной SQLite-БД.

⚠️ SSRF: перед каждым запросом (включая каждый редирект) хост резолвится и все его
IP проверяются на принадлежность приватным/loopback/link-local/reserved диапазонам;
схема — только http/https; тело ограничено по размеру; редиректы ограничены и
проверяются вручную (auto-redirect отключён). Остаточный риск DNS-rebinding (TOCTOU
между нашим resolve и resolve внутри aiohttp) приемлем для данного контекста —
фетчим только публичные веб-страницы, тело не возвращается клиенту целиком.
"""
import asyncio
import html as _html
import ipaddress
import os
import re
import socket
import sqlite3
import threading
import time
from urllib.parse import urljoin, urlparse

import aiohttp

_DB_PATH = "data/link_cache.db"
_lock = threading.Lock()
_tables_created = False

_MAX_BYTES = 512 * 1024            # читаем максимум 512 КБ <head>
_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4)
_MAX_REDIRECTS = 3
_UA = "Mozilla/5.0 (compatible; DailySalesBot/1.0; +link-preview)"

_OK_TTL = 7 * 24 * 3600            # успешный кэш — 7 дней
_FAIL_TTL = 60 * 60               # неудача кэшируется 1 час (не долбить)

_URL_RE = re.compile(r'https?://[^\s<>"\'`]+', re.IGNORECASE)
_FIELD_MAX = 500                   # обрезка title/description
_IMG_MAX = 1000


# ─────────────────────────── кэш ────────────────────────────────────────────
def _ensure_tables() -> None:
    global _tables_created
    if _tables_created:
        return
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS link_previews (
                url        TEXT PRIMARY KEY,
                ok         INTEGER NOT NULL DEFAULT 0,
                title      TEXT DEFAULT '',
                description TEXT DEFAULT '',
                image      TEXT DEFAULT '',
                site_name  TEXT DEFAULT '',
                fetched_at REAL NOT NULL
            )
        """)
        conn.commit()
        _tables_created = True
    finally:
        conn.close()


def _cache_get(url: str):
    _ensure_tables()
    with _lock:
        conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
        try:
            row = conn.execute(
                "SELECT ok, title, description, image, site_name, fetched_at "
                "FROM link_previews WHERE url=?", (url,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    ok, title, desc, image, site, fetched_at = row
    age = time.time() - (fetched_at or 0)
    if (ok and age > _OK_TTL) or (not ok and age > _FAIL_TTL):
        return None
    if not ok:
        return {"ok": False}
    return {
        "ok": True, "title": title or "", "description": desc or "",
        "image": image or "", "site_name": site or "", "url": url,
    }


def _cache_put(url: str, data: dict) -> None:
    _ensure_tables()
    with _lock:
        conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO link_previews "
                "(url, ok, title, description, image, site_name, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    url, 1 if data.get("ok") else 0,
                    (data.get("title") or "")[:_FIELD_MAX],
                    (data.get("description") or "")[:_FIELD_MAX],
                    (data.get("image") or "")[:_IMG_MAX],
                    (data.get("site_name") or "")[:_FIELD_MAX],
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


# ─────────────────────────── SSRF ───────────────────────────────────────────
def _host_is_public(host: str) -> bool:
    """True только если ВСЕ резолвленные IP хоста — публичные."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        ip = info[4][0]
        # отрезаем zone-id у IPv6 (fe80::1%eth0)
        ip = ip.split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast
                or addr.is_unspecified):
            return False
    return True


def _url_is_safe(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme.lower() not in ("http", "https"):
        return False
    if not p.hostname:
        return False
    return _host_is_public(p.hostname)


def extract_first_url(text: str) -> str:
    """Первая http(s)-ссылка в тексте или ''. Завершающая пунктуация срезается."""
    if not text:
        return ""
    m = _URL_RE.search(text)
    if not m:
        return ""
    url = m.group(0).rstrip('.,!?;:)]}»"\'')
    return url if len(url) <= 2048 else ""


# ─────────────────────────── парсинг OG ─────────────────────────────────────
def _meta(content_html: str, *keys: str) -> str:
    """Найти <meta property/name="key" content="...">. Возвращает первый match."""
    for key in keys:
        pat = re.compile(
            r'<meta[^>]+(?:property|name)\s*=\s*["\']' + re.escape(key) +
            r'["\'][^>]*>', re.IGNORECASE)
        m = pat.search(content_html)
        if not m:
            continue
        cm = re.search(r'content\s*=\s*["\']([^"\']*)["\']', m.group(0), re.IGNORECASE)
        if cm and cm.group(1).strip():
            return _html.unescape(cm.group(1).strip())
    return ""


def _parse_og(content_html: str, base_url: str) -> dict:
    title = _meta(content_html, "og:title", "twitter:title")
    if not title:
        tm = re.search(r'<title[^>]*>(.*?)</title>', content_html,
                       re.IGNORECASE | re.DOTALL)
        if tm:
            title = _html.unescape(re.sub(r'\s+', ' ', tm.group(1)).strip())
    desc = _meta(content_html, "og:description", "twitter:description", "description")
    image = _meta(content_html, "og:image", "twitter:image", "twitter:image:src")
    site = _meta(content_html, "og:site_name")
    if image:
        try:
            image = urljoin(base_url, image)
            if not _url_is_safe(image):
                image = ""
        except Exception:
            image = ""
    if not site:
        try:
            site = urlparse(base_url).hostname or ""
        except Exception:
            site = ""
    return {
        "ok": bool(title or desc or image),
        "title": title, "description": desc, "image": image,
        "site_name": site, "url": base_url,
    }


# ─────────────────────────── фетч ───────────────────────────────────────────
async def _fetch(url: str) -> dict:
    """Скачать страницу с ручной валидацией каждого редиректа. {'ok':False} при отказе."""
    cur = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not _url_is_safe(cur):
            return {"ok": False}
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as sess:
                async with sess.get(
                    cur, allow_redirects=False,
                    headers={"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8"},
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location", "")
                        if not loc:
                            return {"ok": False}
                        cur = urljoin(cur, loc)
                        continue
                    if resp.status != 200:
                        return {"ok": False}
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "html" not in ctype and "text" not in ctype:
                        return {"ok": False}
                    buf = b""
                    async for chunk in resp.content.iter_chunked(16384):
                        buf += chunk
                        if len(buf) >= _MAX_BYTES:
                            break
                    text = buf.decode(resp.charset or "utf-8", errors="replace")
                    # парсим только <head> если он есть — экономит regex
                    head_end = text.lower().find("</head>")
                    head = text[: head_end + 7] if head_end != -1 else text
                    return _parse_og(head, cur)
        except Exception:
            return {"ok": False}
    return {"ok": False}


async def get_preview(url: str) -> dict | None:
    """Главная точка входа: кэш → фетч → кэш. None если ссылка небезопасна/нет превью."""
    url = (url or "").strip()
    if not url or len(url) > 2048:
        return None
    if not _url_is_safe(url):
        return None
    cached = _cache_get(url)
    if cached is not None:
        return cached if cached.get("ok") else None
    data = await _fetch(url)
    _cache_put(url, data)
    return data if data.get("ok") else None
