"""Optional: render the page in headless Chromium to see what a browser sees after JavaScript.

Used for two things:
  1. Compare with the raw HTML (what most AI bots read, since they don't run JavaScript).
  2. Record network requests, to detect tracking pixels loaded via Tag Manager.
"""
import functools
import os
import urllib.parse

from .fetch import host_is_public


@functools.lru_cache(maxsize=512)
def _public(host):
    return host_is_public(host)


def render_page(url, timeout_ms=25000):
    if os.environ.get("RENDER_JS", "1") != "1":
        return {"ok": False, "error": "disabled"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}

    requests_seen = []
    try:
        with sync_playwright() as p:
            launch = {"headless": True}
            exe = os.environ.get("CHROMIUM_PATH")
            if exe:
                launch["executable_path"] = exe
            browser = p.chromium.launch(**launch)
            ctx = browser.new_context(locale="he-IL", viewport={"width": 1280, "height": 900})
            page = ctx.new_page()

            def guard(route):
                req_url = route.request.url
                host = urllib.parse.urlparse(req_url).hostname
                if req_url.startswith(("http://", "https://")) and not _public(host):
                    return route.abort()
                requests_seen.append(req_url)
                return route.continue_()

            page.route("**/*", guard)
            page.goto(url, wait_until="load", timeout=timeout_ms)
            page.wait_for_timeout(3000)  # let tag managers fire
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
            html = page.content()
            browser.close()
        return {"ok": True, "text": text, "html": html, "requests": requests_seen}
    except Exception as e:  # rendering is best-effort
        return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:200], "requests": requests_seen}
