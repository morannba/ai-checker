"""Layer 2 (can AI read the site) and the short marketing-tags check."""
import json
import re
import urllib.parse

from bs4 import BeautifulSoup

from .fetch import decode, safe_get

WORD_RE = re.compile(r"[\w֐-׿'\"-]+", re.U)
IL_PHONE_RE = re.compile(r"(?:\+?972[-\s]?|0)(?:[23489]|5\d|7\d)[-\s]?\d{3}[-\s]?\d{4}")


def visible_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    body = soup.body or soup
    return " ".join(body.stripped_strings)


def word_count(text):
    return len(WORD_RE.findall(text or ""))


def readability(raw_html, rendered, business_name=""):
    raw_text = visible_text_from_html(raw_html)
    raw_words = word_count(raw_text)
    out = {"raw_words": raw_words, "rendered_words": None, "ratio": None,
           "phone_in_raw": bool(IL_PHONE_RE.search(raw_text)),
           "name_in_raw": bool(business_name and business_name.strip().lower() in raw_text.lower()),
           "raw_sample": raw_text[:300]}
    if rendered.get("ok"):
        rw = word_count(rendered.get("text", ""))
        out["rendered_words"] = rw
        out["ratio"] = round(raw_words / rw, 2) if rw else None
        out["phone_in_rendered"] = bool(IL_PHONE_RE.search(rendered.get("text", "")))
    return out


def structured_data(raw_html):
    soup = BeautifulSoup(raw_html, "html.parser")
    types = []
    for s in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)})[:15]:
        try:
            data = json.loads(s.string or s.get_text())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                t = obj.get("@type")
                if t:
                    types.extend(t if isinstance(t, list) else [t])
                stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
            elif isinstance(obj, list):
                stack.extend(obj)
    business_types = [t for t in types if isinstance(t, str) and (
        t in ("Organization", "LocalBusiness", "Corporation", "ProfessionalService")
        or t.endswith(("Business", "Store", "Restaurant", "Clinic", "Dentist", "Attorney", "Physician", "Service")))]
    return {"types": sorted(set(map(str, types))), "has_business_schema": bool(business_types)}


def llms_txt(final_url):
    res = safe_get(urllib.parse.urljoin(final_url, "/llms.txt"), max_bytes=100_000)
    if res["status"] != 200:
        return False
    body = decode(res["content"]).lstrip().lower()
    # Many sites return their HTML homepage for unknown paths; that is not an llms.txt.
    return bool(body) and not body.startswith(("<!doctype", "<html"))


# --- marketing tags -------------------------------------------------------
TAGS = {
    "Google Analytics 4": [r"googletagmanager\.com/gtag/js\?id=G-", r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]G-",
                           r"google-analytics\.com/g/collect", r"analytics\.google\.com/g/collect"],
    "Google Tag Manager": [r"googletagmanager\.com/gtm\.js", r"\bGTM-[A-Z0-9]{4,}"],
    "Google Ads (מעקב המרות)": [r"googletagmanager\.com/gtag/js\?id=AW-", r"['\"]AW-\d{6,}",
                                r"googleadservices\.com/pagead/conversion", r"googleads\.g\.doubleclick\.net"],
    "פיקסל של מטא (פייסבוק)": [r"connect\.facebook\.net/[^\"']*/fbevents\.js", r"facebook\.com/tr[/?]",
                               r"fbq\(\s*['\"]init"],
    "פיקסל של טיקטוק": [r"analytics\.tiktok\.com"],
    "LinkedIn Insight": [r"snap\.licdn\.com", r"px\.ads\.linkedin\.com"],
    "Microsoft Clarity / Hotjar": [r"clarity\.ms", r"static\.hotjar\.com"],
}


def marketing(raw_html, rendered):
    haystack = raw_html
    if rendered.get("ok"):
        haystack += "\n" + rendered.get("html", "")
    haystack += "\n" + "\n".join(rendered.get("requests", []))
    found = {name: any(re.search(p, haystack, re.I) for p in pats) for name, pats in TAGS.items()}
    soup = BeautifulSoup(rendered.get("html") or raw_html, "html.parser")
    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    contact = {
        "whatsapp": any(re.search(r"(wa\.me/|api\.whatsapp\.com|whatsapp://)", h, re.I) for h in hrefs),
        "phone_link": any(h.lower().startswith("tel:") for h in hrefs),
        "form": bool(soup.find("form")),
    }
    return {"tags": found, "contact": contact, "rendered": bool(rendered.get("ok"))}
