import csv
import datetime
import json
import os
import re
import threading
import time
import uuid

import requests
from flask import Flask, abort, jsonify, render_template, request

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
from checker.ai_engines import ENGINE_LABELS, enabled_engines  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
DATA = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
os.makedirs(os.path.join(DATA, "reports"), exist_ok=True)
LEADS_CSV = os.path.join(DATA, "leads.csv")

# --- simple in-memory rate limit (per IP and per domain, per day) ---
_lock, _counts = threading.Lock(), {}


def _allow(key, limit):
    day = datetime.date.today().isoformat()
    with _lock:
        n = _counts.get((day, key), 0)
        if n >= limit:
            return False
        _counts[(day, key)] = n + 1
        return True


LEAD_FIELDS = ["time", "report_id", "name", "email", "phone", "business_name", "business_type", "city", "url",
               "ad_platforms", "ad_budget", "site_platform", "goal", "ai_answers", "ai_mentions",
               "top_competitors", "blocked_search_bots", "tags", "utm_source", "utm_campaign", "utm_content"]


def save_lead(row):
    new = not os.path.exists(LEADS_CSV)
    with open(LEADS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
    hook = os.environ.get("LEAD_WEBHOOK_URL", "").strip()  # e.g. a Make / Zapier webhook
    if hook:
        try:
            requests.post(hook, json=row, timeout=10)
        except requests.RequestException:
            pass


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
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(ok=False, error="יש להזין כתובת מייל תקינה."), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    domain = re.sub(r"^https?://(www\.)?", "", get("url").lower()).split("/")[0]
    if not (_allow("ip:" + ip, int(os.environ.get("MAX_PER_IP", "3")))
            and _allow("domain:" + domain, int(os.environ.get("MAX_PER_DOMAIN", "2")))
            and _allow("email:" + email.lower(), int(os.environ.get("MAX_PER_EMAIL", "2")))):
        return jsonify(ok=False, error="הגעתם למספר הבדיקות היומי. נסו שוב מחר, או כתבו לנו."), 429
    custom = [q for q in get("custom_questions", 1000).splitlines() if q.strip()]
    t0 = time.time()
    try:
        report = audit(get("url"), get("business_name"), get("business_type"), get("city"), custom or None,
                       engines=public_engines() or ["none"])
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:  # keep the user-facing message simple
        app.logger.exception("audit failed")
        return jsonify(ok=False, error="הבדיקה נכשלה. נסו שוב בעוד כמה דקות."), 500
    rid = uuid.uuid4().hex[:12]
    report["id"], report["seconds"] = rid, round(time.time() - t0)
    with open(os.path.join(DATA, "reports", rid + ".json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    s = report["summary"]
    save_lead({
        "time": report["generated_at"], "report_id": rid, "name": get("name"), "email": email,
        "phone": get("phone", 40), "business_name": get("business_name"), "business_type": get("business_type"),
        "city": get("city"), "url": report["business"]["url"],
        "ad_platforms": ", ".join(p.get("ad_platforms") or [])[:200] if isinstance(p.get("ad_platforms"), list) else get("ad_platforms"),
        "ad_budget": get("ad_budget"), "site_platform": get("site_platform"), "goal": get("goal"),
        "ai_answers": s["ai_answers"], "ai_mentions": s["ai_mentions"],
        "top_competitors": " | ".join(s["top_competitors"]), "blocked_search_bots": ", ".join(s["blocked_search_bots"]),
        "tags": ", ".join(s["tags"]), "utm_source": get("utm_source"), "utm_campaign": get("utm_campaign"),
        "utm_content": get("utm_content"),
    })
    return jsonify(ok=True, report=report)


if __name__ == "__main__":
    print("Engines enabled:", ", ".join(ENGINE_LABELS[e] for e in enabled_engines()) or "none (set API keys in .env)")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
