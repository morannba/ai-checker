"""Layers 3-4: ask real AI engines (with web search) and record answers + cited sources.

Each engine is enabled only if its API key is set:
  OPENAI_API_KEY     -> ChatGPT (Responses API + web_search)
  GEMINI_API_KEY     -> Gemini (generateContent + google_search grounding)
  ANTHROPIC_API_KEY  -> Claude (Messages API + web_search tool)
Model names and base URLs can be overridden with env vars (see .env.example).
"""
import json
import os
import re
import urllib.parse

import requests

TIMEOUT = 90


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def enabled_engines():
    out = []
    if _env("OPENAI_API_KEY"):
        out.append("chatgpt")
    if _env("GEMINI_API_KEY"):
        out.append("gemini")
    if _env("ANTHROPIC_API_KEY"):
        out.append("claude")
    allow = [e.strip() for e in _env("AI_ENGINES").split(",") if e.strip()]
    return [e for e in out if not allow or e in allow]


ENGINE_LABELS = {"chatgpt": "ChatGPT", "gemini": "Gemini", "claude": "Claude"}


def _host(url):
    try:
        h = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


# --- ChatGPT ---------------------------------------------------------------
def _ask_openai(question, city, use_search=True, country="IL"):
    base = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": "Bearer " + _env("OPENAI_API_KEY"), "Content-Type": "application/json"}
    body = {"model": _env("OPENAI_MODEL", "gpt-4.1-mini"), "input": question}
    tool_types = ["web_search", "web_search_preview"] if use_search else [None]
    last_err = None
    for tool_type in tool_types:
        if tool_type:
            tool = {"type": tool_type}
            if city is not None:  # None = customers from anywhere, so no location hint
                tool["user_location"] = {"type": "approximate"}
                if country:
                    tool["user_location"]["country"] = country
                if city:
                    tool["user_location"]["city"] = city
            body["tools"] = [tool]
        r = requests.post(base + "/responses", headers=headers, json=body, timeout=TIMEOUT)
        if r.status_code == 400 and tool_type == "web_search":
            last_err = r.text[:300]
            continue  # older accounts/models only know web_search_preview
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:300]}")
        data = r.json()
        text, cites = [], []
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text.append(c.get("text", ""))
                    for a in c.get("annotations", []) or []:
                        if a.get("type") == "url_citation" and a.get("url"):
                            cites.append({"url": a["url"], "title": a.get("title", "")})
        return "\n".join(text).strip(), cites
    raise RuntimeError(f"OpenAI: {last_err}")


# --- Gemini ----------------------------------------------------------------
def _ask_gemini(question, city, use_search=True, country="IL"):
    base = _env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model = _env("GEMINI_MODEL", "gemini-2.5-flash")
    body = {"contents": [{"role": "user", "parts": [{"text": question}]}]}
    if use_search:
        body["tools"] = [{"google_search": {}}]
    r = requests.post(f"{base}/models/{model}:generateContent",
                      headers={"x-goog-api-key": _env("GEMINI_API_KEY"), "Content-Type": "application/json"},
                      json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    data = r.json()
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    cites = []
    for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks", []) or []:
        web = ch.get("web") or {}
        # Gemini returns a redirect URI; the title is usually the source domain.
        if web.get("uri") or web.get("title"):
            cites.append({"url": web.get("uri", ""), "title": web.get("title", ""), "domain": web.get("title", "")})
    return text.strip(), cites


# --- Claude ----------------------------------------------------------------
def _ask_claude(question, city, use_search=True, country="IL"):
    base = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    body = {"model": _env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"), "max_tokens": 1500,
            "messages": [{"role": "user", "content": question}]}
    if use_search:
        tool = {"type": _env("ANTHROPIC_SEARCH_TOOL", "web_search_20250305"), "name": "web_search", "max_uses": 3}
        if city is not None:
            tool["user_location"] = {"type": "approximate"}
            if country:
                tool["user_location"]["country"] = country
            if country == "IL":
                tool["user_location"]["timezone"] = "Asia/Jerusalem"
            if city:
                tool["user_location"]["city"] = city
        body["tools"] = [tool]
    r = requests.post(base + "/v1/messages", json=body, timeout=TIMEOUT,
                      headers={"x-api-key": _env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01",
                               "content-type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"Claude {r.status_code}: {r.text[:300]}")
    text, cites = [], []
    for block in r.json().get("content", []):
        if block.get("type") == "text":
            text.append(block.get("text", ""))
            for c in block.get("citations") or []:
                if c.get("url"):
                    cites.append({"url": c["url"], "title": c.get("title", "")})
    return "".join(text).strip(), cites


_ASK = {"chatgpt": _ask_openai, "gemini": _ask_gemini, "claude": _ask_claude}


def ask(engine, question, city="", use_search=True, country="IL"):
    """Returns dict(engine, question, text, citations, error). Never raises.
    city None = no location hint; country None = city only (or nothing)."""
    try:
        text, cites = _ASK[engine](question, city, use_search, country)
        seen, uniq = set(), []
        for c in cites:
            c["domain"] = c.get("domain") or _host(c["url"])
            key = c["url"] or c["domain"]
            if key and key not in seen:
                seen.add(key)
                uniq.append(c)
        return {"engine": engine, "question": question, "text": clean_text(text), "citations": uniq, "error": None}
    except Exception as e:
        return {"engine": engine, "question": question, "text": "", "citations": [], "error": str(e)[:300]}


# --- helpers ----------------------------------------------------------------
KNOW_PROMPT = (
    "An AI assistant was asked: \"{question}\"\n\nThe business is the one behind the website {domain}. "
    "This is what that website says about itself:\n<site>\n{site}\n</site>\n\n"
    "Classify the assistant's answer below:\n"
    "- \"match\": it gives concrete, specific information that fits the business of this website.\n"
    "- \"other\": its main description is of a different business or person (e.g. same or similar name, "
    "but different people, services or history than the website shows).\n"
    "- \"none\": it says it has no information, or only repeats the name or domain.\n"
    "Reply with JSON only: {{\"verdict\": \"match\"}}, {{\"verdict\": \"other\"}} or {{\"verdict\": \"none\"}}"
    "\n\nANSWER:\n{answer}"
)


def knows_business(answer, question, engine, domain="", site_text=""):
    """match / other / none: does the answer describe the business of this site, a namesake, or nothing."""
    if not answer:
        return "none"
    res = ask(engine, KNOW_PROMPT.format(question=question, domain=domain, site=(site_text or "(not available)")[:2500],
                                         answer=answer[:6000]), use_search=False)
    m = re.search(r"\{.*\}", res["text"] or "", re.S)
    try:
        v = json.loads(m.group(0)).get("verdict") if m else None
    except (ValueError, AttributeError):
        v = None
    return v if v in ("match", "other", "none") else "none"


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"בע[\"״']?מ|\bltd\.?|\binc\.?", " ", s)
    return re.sub(r"[\s\"'״׳.,\-–()]+", " ", s).strip()


def domain_root(domain):
    parts = (domain or "").lower().split(".")
    # example.co.il -> example ; shop.example.com -> example
    if len(parts) >= 3 and parts[-2] in ("co", "org", "net", "ac", "gov", "muni"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def _compact(s):
    return re.sub(r"[^\w]", "", (s or "").lower())


# Hebrew prefixes that attach to a name: ה, ב, ל, מ, ו, ש, כ (and pairs like "וב", "שה").
_HE_PREFIX = r"(?:[והבלמשכ]{1,2})?"


def name_in_text(name, text):
    """Whole-word match of a normalized name inside normalized text, allowing Hebrew prefixes."""
    if not name or len(name) < 2:
        return False
    return re.search(r"(?<!\w)" + _HE_PREFIX + re.escape(name) + r"(?!\w)", text) is not None


def own_domain(cite_domain, domain):
    d, dom = (cite_domain or "").lower(), (domain or "").lower()
    return bool(d and dom) and (d == dom or d.endswith("." + dom))


def is_mentioned(result, business_name, domain):
    name, root = _norm(business_name), _compact(domain_root(domain))
    text = _norm(result.get("text"))
    by_name = name_in_text(name, text)
    words = text.split()
    # the domain root written as 1-3 words, e.g. "cohen-cpa" / "Cohen CPA" for cohen-cpa.co.il
    by_domain = len(root) >= 4 and (domain.lower() in (result.get("text") or "").lower() or any(
        _compact("".join(words[i:i + n])) == root for n in (1, 2, 3) for i in range(len(words))))
    by_cite = any(own_domain(c.get("domain"), domain) for c in result.get("citations", []))
    return by_name or by_domain or by_cite, by_cite


_MD_CITE = re.compile(r"\s*\(\[[^\]]*\]\([^)]*\)\)")      # " ([site.co.il](https://...))"
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)[^)]*\)")  # "[text](https://...)" -> "text"


def clean_text(text):
    """Remove inline citation links the engines put in the answer; sources are listed separately."""
    text = _MD_LINK.sub(r"\1", _MD_CITE.sub("", text or ""))
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)  # **bold**
    return re.sub(r"(?m)^#{1,6}\s+", "", text).strip()  # "## heading"
