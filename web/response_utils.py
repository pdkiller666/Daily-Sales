"""Shared HTTP response helpers for web/routes/."""
from urllib.parse import quote as _url_quote


def content_disposition(filename: str) -> str:
    """Build a latin-1-safe Content-Disposition header value.

    HTTP headers are encoded as latin-1, so a plain ``filename="..."`` field
    crashes (500) whenever the name contains Cyrillic or other non-ASCII
    characters.  This helper emits both an ASCII-only fallback (which all
    browsers accept) and the RFC 5987 ``filename*=UTF-8''...`` form (which
    modern browsers prefer for the actual filename shown in the Save dialog).

    Usage::

        headers={"Content-Disposition": content_disposition("Товар.xlsx")}
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download"
    quoted = _url_quote(filename)
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'
