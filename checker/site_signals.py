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


# --- digital stack: ads, measurement, CRM, site platform ------------------------
# Detected from the page HTML and the requests the browser made. "Not found" only means we did not see it
# on the homepage: a CRM can be connected on the server side, so the report words those as estimates.
STACK = {
    "פרסום": {
        "פיקסל של מטא (פייסבוק)": [r"connect\.facebook\.net/[^\"']*/fbevents\.js", r"facebook\.com/tr[/?]",
                                   r"fbq\(\s*['\"]init"],
        "Google Ads (מעקב המרות)": [r"googletagmanager\.com/gtag/js\?id=AW-", r"['\"]AW-\d{6,}",
                                    r"googleadservices\.com/pagead/conversion", r"googleads\.g\.doubleclick\.net"],
        "LinkedIn Insight": [r"snap\.licdn\.com", r"px\.ads\.linkedin\.com"],
        "פיקסל של טיקטוק": [r"analytics\.tiktok\.com"],
        "Taboola / Outbrain": [r"cdn\.taboola\.com", r"trc\.taboola\.com", r"amplify\.outbrain\.com",
                               r"widgets\.outbrain\.com"],
    },
    "מדידה": {
        "Google Analytics 4": [r"googletagmanager\.com/gtag/js\?id=G-", r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]G-",
                               r"google-analytics\.com/g/collect", r"analytics\.google\.com/g/collect"],
        "Google Tag Manager": [r"googletagmanager\.com/gtm\.js", r"\bGTM-[A-Z0-9]{4,}"],
        "Microsoft Clarity / Hotjar": [r"clarity\.ms", r"static\.hotjar\.com"],
    },
    "CRM ואוטומציה": {
        "HubSpot": [r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"js\.hs-analytics\.net"],
        "Salesforce / Pardot": [r"pi\.pardot\.com", r"go\.pardot\.com", r"webto\.salesforce"],
        "Zoho": [r"salesiq\.zoho", r"zohopublic\.", r"crm\.zoho\.", r"forms\.zohopublic"],
        "Fireberry / Powerlink": [r"fireberry\.com", r"powerlink\.co\.il"],
        "monday.com": [r"forms\.monday\.com", r"wkf\.ms/"],
        "Pipedrive": [r"pipedrivewebforms\.com", r"leadbooster-chat\.pipedrive", r"webforms\.pipedrive"],
        "ActiveCampaign": [r"trackcmp\.net", r"\.activehosted\.com"],
        "Mailchimp": [r"chimpstatic\.com", r"list-manage\.com"],
        "Klaviyo": [r"static\.klaviyo\.com", r"klaviyo\.com/onsite"],
    },
}
TAGS = {name: pats for group in STACK.values() for name, pats in group.items()}

PLATFORMS = {
    "WordPress": [r"/wp-content/", r"/wp-includes/"],
    "Wix": [r"static\.wixstatic\.com", r"static\.parastorage\.com"],
    "Shopify": [r"cdn\.shopify\.com"],
    "Webflow": [r"assets\.website-files\.com", r"\bdata-wf-page\b"],
    "Squarespace": [r"static1\.squarespace\.com"],
    "SITE123": [r"site123\.me"],
}
CHAT = [r"code\.tidio\.co", r"widget\.intercom\.io", r"static\.zdassets\.com", r"embed\.tawk\.to",
        r"client\.crisp\.chat"]


def _found(pats, haystack):
    return any(re.search(p, haystack, re.I) for p in pats)


def marketing(raw_html, rendered):
    haystack = raw_html
    if rendered.get("ok"):
        haystack += "\n" + rendered.get("html", "")
    haystack += "\n" + "\n".join(rendered.get("requests", []))
    stack = {group: {name: _found(pats, haystack) for name, pats in tools.items()} for group, tools in STACK.items()}
    found = {name: ok for group in stack.values() for name, ok in group.items()}
    soup = BeautifulSoup(rendered.get("html") or raw_html, "html.parser")
    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    contact = {
        "whatsapp": any(re.search(r"(wa\.me/|api\.whatsapp\.com|whatsapp://)", h, re.I) for h in hrefs),
        "phone_link": any(h.lower().startswith("tel:") for h in hrefs),
        "form": bool(soup.find("form")),
        "chat": _found(CHAT, haystack),
    }
    platform = [name for name, pats in PLATFORMS.items() if _found(pats, haystack)]
    return {"tags": found, "stack": stack, "platform": platform, "contact": contact,
            "rendered": bool(rendered.get("ok"))}


# --- SEO basics ---------------------------------------------------------------
def seo_basics(raw_html, final_url):
    soup = BeautifulSoup(raw_html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = (meta.get("content") or "").strip() if meta else ""
    h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    viewport = bool(soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}))
    res = safe_get(urllib.parse.urljoin(final_url, "/sitemap.xml"), max_bytes=200_000)
    body = decode(res["content"]).lstrip().lower() if res["status"] == 200 else ""
    sitemap = body.startswith("<?xml") or "<urlset" in body or "<sitemapindex" in body
    return {"title": title, "description": desc, "h1": len(h1), "viewport": viewport, "sitemap": sitemap}
