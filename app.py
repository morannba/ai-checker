import csv
import datetime
import json
import os
import re
import smtplib
import threading
import time
import uuid
from email.message import EmailMessage

import requests
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


LEAD_FIELDS = ["time", "report_id", "name", "email", "phone", "business_name", "business_type", "city", "scope", "url",
               "ad_platforms", "ad_budget", "site_platform", "goal", "ai_answers", "ai_mentions",
               "blocked_search_bots", "tags", "utm_source", "utm_campaign", "utm_content", "report_url"]


def save_lead(row):
    new = not os.path.exists(LEADS_CSV)
    with open(LEADS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
    if os.environ.get("LEAD_EMAIL_TO", "").strip():
        # in the background: a slow mail server shouldn't hold up the visitor's report
        threading.Thread(target=email_lead, args=(row,), daemon=True).start()
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


def email_lead(row):
    """Send each lead by SMTP (e.g. Gmail with an app password). Settings in .env; failures only go to the log."""
    try:
        msg = EmailMessage()
        msg["Subject"] = f"ליד חדש מבדיקת AI: {row.get('business_name', '')} ({row.get('ad_budget') or 'בלי תקציב'})"
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
            "cta_url": os.environ.get("CTA_URL", ""), "cta_text": os.environ.get("CTA_TEXT", "לקביעת שיחה")}


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


@app.post("/api/audit")
def api_audit():
    p = request.get_json(silent=True) or {}
    get = lambda k, n=200: str(p.get(k, "") or "").strip()[:n]  # noqa: E731
    email = get("email")
    if not get("url") or not get("business_name") or not get("business_type"):
        return jsonify(ok=False, error="יש למלא כתובת אתר, שם העסק ותחום."), 400
    if same_as_name(get("business_type"), get("business_name")):
        # the questions would contain the business name, so ChatGPT just echoes it back
        return jsonify(ok=False, error="בשדה \"מה העסק עושה\" כתבו את התחום כמו שלקוח היה מחפש (למשל: יועץ שיווק דיגיטלי), ולא את שם העסק."), 400
    if get("scope") == "local" and not get("city"):
        return jsonify(ok=False, error="כתבו באיזו עיר או אזור העסק פועל, או בחרו שהלקוחות מגיעים מכל הארץ."), 400
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(ok=False, error="יש להזין כתובת מייל תקינה."), 400
    domain = re.sub(r"^https?://(www\.)?", "", get("url").lower()).split("/")[0]
    taken = _take([("ip:" + client_ip(), int(os.environ.get("MAX_PER_IP", "3"))),
                   ("domain:" + domain, int(os.environ.get("MAX_PER_DOMAIN", "2"))),
                   ("email:" + email.lower(), int(os.environ.get("MAX_PER_EMAIL", "2")))])
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
    save_lead({
        "time": report["generated_at"], "report_id": rid, "name": get("name"), "email": email,
        "phone": get("phone", 40), "business_name": get("business_name"), "business_type": get("business_type"),
        "city": get("city"), "scope": report["business"]["scope"], "url": report["business"]["url"],
        "ad_platforms": ", ".join(p.get("ad_platforms") or [])[:200] if isinstance(p.get("ad_platforms"), list) else get("ad_platforms"),
        "ad_budget": get("ad_budget"), "site_platform": get("site_platform"), "goal": get("goal"),
        "ai_answers": s["ai_answers"], "ai_mentions": s["ai_mentions"],
        "blocked_search_bots": ", ".join(s["blocked_search_bots"]),
        "tags": ", ".join(s["tags"]), "utm_source": get("utm_source"), "utm_campaign": get("utm_campaign"),
        "utm_content": get("utm_content"), "report_url": request.host_url.rstrip("/") + "/r/" + rid,
    })
    return jsonify(ok=True, report=report)


if __name__ == "__main__":
    print("Engines enabled:", ", ".join(ENGINE_LABELS[e] for e in enabled_engines()) or "none (set API keys in .env)")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
