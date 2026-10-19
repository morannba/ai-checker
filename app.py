import csv
import datetime
import json
import os
import re
import smtplib
import threading
import time
import urllib.parse
import uuid
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(BASE, ".env")):
    """Minimal .env loader (KEY=value per line), so no extra dependency is needed."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
from checker.audit import audit  # noqa: E402  (after env is loaded)
from checker.ai_engines import ENGINE_LABELS, _norm, enabled_engines  # noqa: E402
from checker.fetch import decode, normalize_url, safe_get  # noqa: E402

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # links use https://our-domain behind Render
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
DATA = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(os.path.join(DATA, "reports"), exist_ok=True)
LEADS_CSV = os.path.join(DATA, "leads.csv")

# --- simple in-memory rate limit (per IP and per domain, per day) ---
_lock, _counts = threading.Lock(), {}


def _take(limits):
    """limits: [(key, max_per_day)]. Takes one from each only if all have room. Returns the taken keys or None."""
    day = datetime.date.today().isoformat()
    with _lock:
        if any(_counts.get((day, k), 0) >= n for k, n in limits):
            return None
        keys = [(day, k) for k, _ in limits]
        for k in keys:
            _counts[k] = _counts.get(k, 0) + 1
        return keys


def _give_back(keys):
    with _lock:
        for k in keys or []:
            _counts[k] = max(0, _counts.get(k, 0) - 1)


def client_ip():
    """The client IP as seen by our own proxy. X-Forwarded-For entries left of the proxies we trust are
    written by the client and can be faked, so we count PROXY_HOPS entries from the right
    (1 on Render/Railway; 2 if Cloudflare's proxy is also in front)."""
    hops = int(os.environ.get("PROXY_HOPS", "1"))
    parts = [x.strip() for x in request.headers.get("X-Forwarded-For", "").split(",") if x.strip()]
    if hops > 0 and len(parts) >= hops:
        return parts[-hops]
    return request.remote_addr or ""


# Each audit opens a Chromium and up to 6 AI calls; cap how many run at once so a burst can't exhaust memory.
_audit_slots = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT_AUDITS", "3")))


EXTRA_FIELDS = ["site_platform", "ad_platforms", "ad_budget", "goal"]  # asked while the check runs (ad_budget is now required up front)
_extras, _saved = {}, {}  # lead_key -> answers given so far / -> the saved lead, for answers that come in late


def _extra_values(p):
    out = {}
    for k in EXTRA_FIELDS:
        v = p.get(k)
        v = ", ".join(str(x) for x in v if x)[:200] if isinstance(v, list) else str(v or "").strip()[:200]
        if v:
            out[k] = v
    return out


def _prune(store, max_age=3 * 3600):
    now = time.time()
    for k in [k for k, v in store.items() if now - v["t"] > max_age]:
        store.pop(k, None)


LEAD_FIELDS = ["time", "report_id", "name", "email", "phone", "business_name", "business_type", "city", "scope", "url",
               "ad_platforms", "ad_budget", "site_platform", "goal", "ai_answers", "ai_mentions",
               "blocked_search_bots", "tags", "utm_source", "utm_campaign", "utm_content", "report_url"]


def save_lead(row, update=False):
    """update=True: extra answers for a lead already saved; written as another row with the same report_id."""
    new = not os.path.exists(LEADS_CSV)
    with open(LEADS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
    if os.environ.get("LEAD_EMAIL_TO", "").strip():
        # in the background: a slow mail server shouldn't hold up the visitor's report
        threading.Thread(target=email_lead, args=(row, update), daemon=True).start()
    hook = os.environ.get("LEAD_WEBHOOK_URL", "").strip()  # e.g. a Make / Zapier webhook
    if hook:
        try:
            requests.post(hook, json=row, timeout=10)
        except requests.RequestException:
            pass


def same_as_name(business_type, business_name):
    t, n = _norm(business_type), _norm(business_name)
    return bool(t and n) and (n in t or t in n)


LEAD_LABELS = [("name", "שם"), ("phone", "טלפון"), ("email", "מייל"), ("business_name", "עסק"), ("url", "אתר"),
               ("business_type", "תחום"), ("scope", "לקוחות"), ("city", "עיר"), ("ad_budget", "תקציב פרסום"),
               ("ad_platforms", "מפרסם ב"), ("site_platform", "האתר בנוי על"), ("goal", "מה הכי חשוב"),
               ("ai_mentions", "הופעות ב-AI"), ("ai_answers", "מתוך תשובות"), ("blocked_search_bots", "בוטים חסומים"),
               ("tags", "כלי מדידה"), ("utm_source", "utm_source"), ("utm_campaign", "utm_campaign"),
               ("utm_content", "utm_content"), ("report_url", "הדוח")]


SCOPE_LABELS = {"local": "מהאזור", "israel": "מכל הארץ", "global": "גם מחו\"ל / אונליין"}


def email_lead(row, update=False):
    """Send each lead by SMTP (e.g. Gmail with an app password). Settings in .env; failures only go to the log."""
    try:
        msg = EmailMessage()
        head = "עוד פרטים לליד" if update else "ליד חדש מבדיקת AI"
        msg["Subject"] = f"{head}: {row.get('business_name', '')} ({row.get('ad_budget') or 'בלי תקציב'})"
        msg["From"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
        msg["To"] = os.environ["LEAD_EMAIL_TO"]
        if row.get("email"):
            msg["Reply-To"] = row["email"]
        shown = dict(row, scope=SCOPE_LABELS.get(row.get("scope"), row.get("scope", "")))
        msg.set_content("\n".join(f"{label}: {shown.get(k, '')}" for k, label in LEAD_LABELS if shown.get(k, "") != ""))
        host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
        smtp = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
        with smtp:
            if port != 465:
                smtp.starttls()
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            smtp.send_message(msg)
    except Exception:
        app.logger.exception("lead email failed")


def public_engines():
    """Engines used in the free public check (default: ChatGPT only). Others are offered as an upsell."""
    wanted = [e.strip() for e in os.environ.get("PUBLIC_ENGINES", "chatgpt").split(",") if e.strip()]
    return [e for e in enabled_engines() if e in wanted]


def page_ctx(report=None):
    used = (report or {}).get("ai", {}).get("engines") or public_engines()
    more = [ENGINE_LABELS[e] for e in ENGINE_LABELS if e not in used]
    return {"report": report, "engines": [ENGINE_LABELS[e] for e in used], "more_engines": more,
            "cta_url": os.environ.get("CTA_URL", ""), "cta_text": os.environ.get("CTA_TEXT", "לקביעת שיחה"),
            # Growth With Moran's Meta pixel (public id); set META_PIXEL_ID="" to turn it off
            "pixel_id": os.environ.get("META_PIXEL_ID", "824516214280119")}


@app.get("/")
def index():
    return render_template("index.html", **page_ctx())


@app.get("/r/<rid>")
def saved_report(rid):
    if not re.fullmatch(r"[a-f0-9]{12}", rid):
        abort(404)
    path = os.path.join(DATA, "reports", rid + ".json")
    if not os.path.exists(path):
        abort(404)
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    return render_template("index.html", **page_ctx(report))


GENERIC_TITLE = re.compile(r"^(דף הבית|עמוד הבית|ראשי|בית|home|homepage|home page|welcome|ברוכים הבאים)$", re.I)


def site_name_from_html(html, domain):
    """Best guess at the business name: og:site_name, else the part of <title> that isn't 'Home' or a tagline."""
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content", "").strip():
        return og["content"].strip()[:80]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    parts = [x.strip() for x in re.split(r"\s[|\-–—:·•]\s|\s?[|•·]\s?", title) if x.strip()]
    parts = [x for x in parts if not GENERIC_TITLE.match(x)]
    if not parts:
        return ""
    root = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    for x in parts:  # the segment that matches the domain, e.g. "Growth With Moran" for growthwithmoran.com
        if root and len(root) >= 4 and re.sub(r"[^a-z0-9]", "", x.lower()) == root:
            return x[:80]
    return min(parts, key=len)[:80] if len(parts) > 1 else parts[0][:80]


@app.get("/api/site-name")
def api_site_name():
    """Prefills the business name from the site's title once the visitor types the address."""
    if not _take([("name:" + client_ip(), int(os.environ.get("MAX_NAME_LOOKUPS", "40")))]):
        return jsonify(name="")
    try:
        url = normalize_url(request.args.get("url", "")[:300])
    except ValueError:
        return jsonify(name="")
    r = safe_get(url, max_bytes=400_000)
    if r["error"] or not r["status"] or r["status"] >= 400:
        return jsonify(name="")
    domain = re.sub(r"^(www\.)?", "", (urllib.parse.urlparse(r["final_url"]).hostname or "").lower())
    try:
        return jsonify(name=site_name_from_html(decode(r["content"]), domain))
    except Exception:
        return jsonify(name="")


@app.post("/api/extra")
def api_extra():
    """Optional questions answered on the waiting screen. Merged into the lead if it isn't saved yet,
    otherwise sent as a follow-up so late answers aren't lost."""
    p = request.get_json(silent=True) or {}
    key = str(p.get("lead_key", ""))
    if not re.fullmatch(r"[a-f0-9]{16,32}", key):
        return jsonify(ok=False), 400
    vals = _extra_values(p)
    with _lock:
        _prune(_extras), _prune(_saved)
        _extras[key] = {"t": time.time(), "v": vals}
        lead = _saved.get(key)
    if lead and vals:
        row = dict(lead["v"], **vals, time=datetime.datetime.now().isoformat(timespec="seconds"))
        save_lead(row, update=True)
    return jsonify(ok=True)


@app.post("/api/audit")
def api_audit():
    p = request.get_json(silent=True) or {}
    get = lambda k, n=200: str(p.get(k, "") or "").strip()[:n]  # noqa: E731
    email, phone = get("email"), get("phone", 40)
    digits = re.sub(r"\D", "", phone)
    # "step" tells the page which of its two steps holds the field to fix
    if not get("url") or not get("business_name") or not get("business_type"):
        return jsonify(ok=False, step=1, error="יש למלא כתובת אתר, שם העסק ותחום."), 400
    if same_as_name(get("business_type"), get("business_name")):
        # the questions would contain the business name, so ChatGPT just echoes it back
        return jsonify(ok=False, step=1, error="בשדה \"מה העסק עושה\" כתבו את התחום כמו שלקוח היה מחפש (למשל: יועץ שיווק דיגיטלי), ולא את שם העסק."), 400
    if get("scope") == "local" and not get("city"):
        return jsonify(ok=False, step=1, error="כתבו באיזו עיר או אזור העסק פועל, או בחרו שהלקוחות מגיעים מכל הארץ."), 400
    if not get("ad_budget"):
        return jsonify(ok=False, step=1, error="בחרו את תקציב הפרסום החודשי."), 400
    if not get("name"):
        return jsonify(ok=False, step=2, error="כתבו את השם שלכם."), 400
    if not 9 <= len(digits) <= 13:
        return jsonify(ok=False, step=2, error="יש להזין מספר טלפון תקין."), 400
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(ok=False, step=2, error="כתובת המייל לא תקינה. אפשר גם להשאיר את השדה ריק."), 400
    domain = re.sub(r"^https?://(www\.)?", "", get("url").lower()).split("/")[0]
    taken = _take([("ip:" + client_ip(), int(os.environ.get("MAX_PER_IP", "3"))),
                   ("domain:" + domain, int(os.environ.get("MAX_PER_DOMAIN", "2"))),
                   ("phone:" + digits[-9:], int(os.environ.get("MAX_PER_PHONE", "2")))])
    if not taken:
        return jsonify(ok=False, error="הגעתם למספר הבדיקות היומי. נסו שוב מחר, או כתבו לנו."), 429
    if not _audit_slots.acquire(timeout=60):
        _give_back(taken)
        return jsonify(ok=False, error="יש כרגע הרבה בדיקות במקביל. נסו שוב בעוד דקה."), 503
    custom = [q for q in get("custom_questions", 1000).splitlines() if q.strip()]
    t0 = time.time()
    try:
        report = audit(get("url"), get("business_name"), get("business_type"), get("city"), custom or None,
                       scope=get("scope") or None,
                       engines=public_engines() or ["none"])
    except ValueError as e:  # bad or unreachable URL: nothing was spent, so the check doesn't count
        _give_back(taken)
        return jsonify(ok=False, error=str(e)), 400
    except Exception:  # keep the user-facing message simple
        app.logger.exception("audit failed")
        return jsonify(ok=False, error="הבדיקה נכשלה. נסו שוב בעוד כמה דקות."), 500
    finally:
        _audit_slots.release()
    rid = uuid.uuid4().hex[:12]
    report["id"], report["seconds"] = rid, round(time.time() - t0)
    with open(os.path.join(DATA, "reports", rid + ".json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    s = report["summary"]
    row = {
        "time": report["generated_at"], "report_id": rid, "name": get("name"), "email": email,
        "phone": phone, "business_name": get("business_name"), "business_type": get("business_type"),
        "city": get("city"), "scope": report["business"]["scope"], "url": report["business"]["url"],
        "ai_answers": s["ai_answers"], "ai_mentions": s["ai_mentions"],
        "blocked_search_bots": ", ".join(s["blocked_search_bots"]),
        "tags": ", ".join(s["tags"]), "utm_source": get("utm_source"), "utm_campaign": get("utm_campaign"),
        "utm_content": get("utm_content"), "report_url": request.host_url.rstrip("/") + "/r/" + rid,
    }
    key = str(p.get("lead_key", ""))
    with _lock:
        row.update(_extra_values(p))  # still accepted in the request itself
        row.update((_extras.pop(key, None) or {}).get("v", {}))  # answered on the waiting screen
        if re.fullmatch(r"[a-f0-9]{16,32}", key):
            _saved[key] = {"t": time.time(), "v": row}
    save_lead(row)
    return jsonify(ok=True, report=report)


if __name__ == "__main__":
    print("Engines enabled:", ", ".join(ENGINE_LABELS[e] for e in enabled_engines()) or "none (set API keys in .env)")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
