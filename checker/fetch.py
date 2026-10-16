"""Safe HTTP fetching for public websites only (basic SSRF protection)."""
import ipaddress
import os
import re
import socket
import urllib.parse

import requests

TIMEOUT = 12
MAX_HTML_BYTES = 1_500_000
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def _allow_private():
    # Only for local tests. Never set this in production.
    return os.environ.get("ALLOW_PRIVATE_HOSTS") == "1"


def host_is_public(host):
    if _allow_private():
        return True
    host = (host or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):  # UnicodeError: host that can't be IDNA-encoded
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


# zero-width and bidi marks that ride along when an address is copied from Hebrew text
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\\s]")


def normalize_url(raw):
    raw = _INVISIBLE.sub("", raw or "")
    if not raw:
        raise ValueError("יש להזין כתובת אתר.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    p = urllib.parse.urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("כתובת האתר לא תקינה.")
    if p.username or p.password:
        raise ValueError("כתובות עם שם משתמש או סיסמה לא נתמכות.")
    try:
        p.hostname.encode("idna")
    except UnicodeError:  # e.g. a Hebrew letter typed next to the English address
        raise ValueError("בכתובת האתר יש תו לא תקין (אולי אות בעברית). בדקו שהכתובת כתובה כולה באנגלית.") from None
    if not host_is_public(p.hostname):
        raise ValueError("אפשר לבדוק רק אתר ציבורי. בדקו שהכתובת נכונה.")
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path or "/", "", p.query, ""))


def safe_get(url, user_agent=BROWSER_UA, max_bytes=MAX_HTML_BYTES):
    """GET that re-validates every redirect. Never raises on HTTP status.

    Returns dict(status, final_url, content, content_type, error).
    """
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
               "Accept-Language": "he,en;q=0.8"}
    current = url
    try:
        for _ in range(6):
            p = urllib.parse.urlparse(current)
            if p.scheme not in ("http", "https") or not host_is_public(p.hostname):
                return {"status": None, "final_url": current, "content": b"", "content_type": "",
                        "error": "הפניה לכתובת לא ציבורית"}
            r = requests.get(current, headers=headers, timeout=TIMEOUT, allow_redirects=False, stream=True)
            if r.is_redirect or r.is_permanent_redirect:
                loc = r.headers.get("Location")
                r.close()
                if not loc:
                    break
                current = urllib.parse.urljoin(current, loc)
                continue
            chunks, size = [], 0
            for chunk in r.iter_content(16384):
                size += len(chunk)
                if size > max_bytes:
                    break
                chunks.append(chunk)
            r.close()
            return {"status": r.status_code, "final_url": current, "content": b"".join(chunks),
                    "content_type": r.headers.get("Content-Type", ""), "error": None}
        return {"status": None, "final_url": current, "content": b"", "content_type": "",
                "error": "יותר מדי הפניות"}
    except requests.RequestException as e:
        return {"status": None, "final_url": current, "content": b"", "content_type": "",
                "error": type(e).__name__}


def decode(content):
    return content.decode("utf-8", "replace")
