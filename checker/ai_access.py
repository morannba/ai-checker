"""Layer 1: can AI bots get into the site? robots.txt rules + a live probe with bot user agents."""
import urllib.parse
import urllib.robotparser

from .fetch import BROWSER_UA, decode, safe_get

# token, company, purpose, kind ("search" = affects answers today, "training" = affects future models)
AI_BOTS = [
    ("OAI-SearchBot", "OpenAI", "חיפוש בתוך ChatGPT", "search"),
    ("ChatGPT-User", "OpenAI", "ChatGPT נכנס לאתר בזמן שיחה", "search"),
    ("GPTBot", "OpenAI", "אימון מודלים", "training"),
    ("Claude-SearchBot", "Anthropic", "חיפוש בתוך Claude", "search"),
    ("Claude-User", "Anthropic", "Claude נכנס לאתר בזמן שיחה", "search"),
    ("ClaudeBot", "Anthropic", "אימון מודלים", "training"),
    ("PerplexityBot", "Perplexity", "חיפוש ב-Perplexity", "search"),
    ("Perplexity-User", "Perplexity", "Perplexity נכנס לאתר בזמן שיחה", "search"),
    ("Googlebot", "Google", "חיפוש גוגל ו-AI Overviews", "search"),
    ("Google-Extended", "Google", "שימוש בתוכן ל-Gemini", "training"),
    ("bingbot", "Microsoft", "חיפוש Bing ו-Copilot", "search"),
]

# Full user-agent strings used for the live probe (as published by each company).
PROBE_UAS = {
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.1; +https://openai.com/gptbot)",
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
}

# Phrases from bot-challenge / block pages. Not "captcha" on its own: many normal pages load reCAPTCHA.
CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "just a moment...", "attention required! | cloudflare",
                     "cf-browser-verification", "access denied", "request blocked")


def is_challenge(content):
    body = decode(content[:20000]).lower()
    return any(m in body for m in CHALLENGE_MARKERS)


def check_robots(final_url):
    robots_url = urllib.parse.urljoin(final_url, "/robots.txt")
    res = safe_get(robots_url, max_bytes=300_000)
    out = {"url": robots_url, "status": res["status"], "error": res["error"], "bots": [], "exists": False}
    if res["error"] or res["status"] is None:
        return out
    text = decode(res["content"]) if res["status"] == 200 else ""
    out["exists"] = res["status"] == 200 and bool(text.strip())
    rp = urllib.robotparser.RobotFileParser()
    if res["status"] in (401, 403):
        rp.disallow_all = True  # same convention Python and most crawlers use
    else:
        rp.parse(text.splitlines())
    path = urllib.parse.urlparse(final_url).path or "/"
    for token, company, purpose, kind in AI_BOTS:
        allowed = rp.can_fetch(token, path)
        out["bots"].append({"bot": token, "company": company, "purpose": purpose, "kind": kind,
                            "allowed": allowed})
    return out


def _looks_blocked(res, base):
    if base["status"] != 200 or is_challenge(base["content"]):
        return False  # we could not get in as a browser either, so there is nothing to compare
    if res["error"]:
        return True
    if res["status"] in (401, 403, 406, 429, 503):
        return True
    return is_challenge(res["content"])


def probe_bots(final_url):
    """Fetch the page as a browser and as AI bots. A difference suggests a firewall/CDN block.

    Caveat: some firewalls verify real bots by IP, so a block here is a strong hint, not proof.
    """
    base = safe_get(final_url, BROWSER_UA, max_bytes=200_000)
    results = []
    for bot, ua in PROBE_UAS.items():
        r = safe_get(final_url, ua, max_bytes=200_000)
        results.append({"bot": bot, "status": r["status"], "blocked": _looks_blocked(r, base)})
    return {"browser_status": base["status"], "results": results}
