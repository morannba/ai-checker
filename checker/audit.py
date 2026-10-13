"""Runs all checks and turns them into a short, plain-Hebrew report (no overall score)."""
import collections
import datetime
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import ai_access, ai_engines, render, site_signals
from .fetch import decode, normalize_url, safe_get

VERIFIED, INFERRED, NOT_TESTED = "נבדק בפועל", "הערכה", "לא נבדק"


def build_questions(business_type, city, custom=None):
    if custom:
        return [q.strip() for q in custom if q.strip()][:5]
    city = city.strip()
    # "בחיפה", but "ב-Haifa": a Hebrew prefix glued to Latin letters reads as a typo
    where = (f"ב{city}" if re.match(r"[\u0590-\u05ff]", city) else f"ב-{city}") if city else "בישראל"
    t = business_type.strip()
    return [
        f"אני מחפש {t} {where}. את מי כדאי לי לבדוק?",
        f"מי ה{t} הכי מומלצים {where} והסביבה?",
        f"תמליץ לי על {t} טוב {where}, עם כמה שמות ולמה.",
    ]


def knowledge_question(name, domain):
    return f"מה אתה יודע על {name} ({domain})? מה הם עושים, איפה הם פועלים ומה אומרים עליהם?"


def run_ai(name, domain, business_type, city, custom_questions=None, engines=None, runs=None):
    available = ai_engines.enabled_engines()
    engines = [e for e in (engines or available) if e in available]
    if not engines:
        return {"engines": [], "skipped": "לא הוגדר מפתח API לאף מנוע AI"}
    runs = runs or int(os.environ.get("AI_RUNS", "2"))
    questions = build_questions(business_type, city, custom_questions)
    jobs = []  # (kind, engine, question)
    for e in engines:
        jobs.append(("knowledge", e, knowledge_question(name, domain)))
        for q in questions:
            jobs += [("recommend", e, q)] * runs
    workers = int(os.environ.get("AI_CONCURRENCY", "6"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda j: (j[0], ai_engines.ask(j[1], j[2], city)), jobs))

    # extract named businesses from each recommendation answer (cheap, no search)
    rec = [r for k, r in results if k == "recommend"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        names = list(pool.map(lambda r: ai_engines.extract_businesses(r["text"], r["engine"]), rec))
    for r, n in zip(rec, names):
        r["businesses"] = n
        r["mentioned"], r["own_site_cited"] = ai_engines.is_mentioned(r, name, domain)
    know = [r for k, r in results if k == "knowledge"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        knows = list(pool.map(lambda r: ai_engines.knows_business(r["text"], r["question"], r["engine"]), know))
    for r, k in zip(know, knows):
        _, r["own_site_cited"] = ai_engines.is_mentioned(r, name, domain)
        r["mentioned"] = k or r["own_site_cited"]
    return {"engines": engines, "questions": questions, "runs": runs, "recommend": rec, "knowledge": know}


def fetch_home(url):
    page = safe_get(url)
    if page["error"] or page["status"] is None:
        raise ValueError("לא הצלחנו להגיע לאתר. בדקו שהכתובת נכונה ושהאתר פעיל.")
    return page


def run_site(page, business_name=""):
    raw_html = decode(page["content"])
    final_url = page["final_url"]
    rendered = render.render_page(final_url)
    return {
        "final_url": final_url,
        "status": page["status"],
        # a firewall / bot challenge answered our plain request, so the raw HTML is not the real page
        "challenged": page["status"] in (401, 403, 429, 503) or ai_access.is_challenge(page["content"]),
        "robots": ai_access.check_robots(final_url),
        "probe": ai_access.probe_bots(final_url),
        "readability": site_signals.readability(raw_html, rendered, business_name),
        "schema": site_signals.structured_data(raw_html),
        "llms_txt": site_signals.llms_txt(final_url),
        "marketing": site_signals.marketing(raw_html, rendered),
        "rendered_ok": bool(rendered.get("ok")),
    }


def audit(url, business_name, business_type, city="", custom_questions=None, engines=None, runs=None):
    url = normalize_url(url)
    domain = urllib.parse.urlparse(url).hostname.removeprefix("www.")
    page = fetch_home(url)  # before any paid AI call: a typo in the URL should cost nothing
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_site = pool.submit(run_site, page, business_name)
        f_ai = pool.submit(run_ai, business_name, domain, business_type, city, custom_questions, engines, runs)
        site, ai = f_site.result(), f_ai.result()
    report = {
        "business": {"name": business_name, "type": business_type, "city": city, "url": url, "domain": domain},
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="minutes"),
        "findings": build_findings(business_name, domain, site, ai),
        "ai": ai,
        "site": site,
    }
    report["summary"] = summarize(report)
    return report


# --- report -------------------------------------------------------------------
L = ai_engines.ENGINE_LABELS


def _times(k):
    return "בתשובה אחת" if k == 1 else f"ב-{k} תשובות"


def F(section, title, body, action="", status=VERIFIED, level="info", items=None):
    return {"section": section, "title": title, "body": body, "action": action, "status": status,
            "level": level, "items": items or []}


def _competitors(ai, business_name, domain):
    own = ai_engines._norm(business_name)
    root = ai_engines._compact(ai_engines.domain_root(domain))
    c = collections.Counter()
    for r in ai.get("recommend", []):
        for n in dict.fromkeys(r.get("businesses", [])):  # once per answer
            nn = ai_engines._norm(n)
            is_self = ai_engines.name_in_text(own, nn) or (len(root) >= 4 and ai_engines._compact(nn) == root)
            if nn and not is_self:
                c[n] += 1
    return c


def _sources(ai, domain):
    c = collections.Counter()
    for r in ai.get("recommend", []):
        for d in {x.get("domain", "").lower() for x in r.get("citations", [])}:
            if d and not ai_engines.own_domain(d, domain):
                c[d] += 1
    return c


def build_findings(name, domain, site, ai):
    out = []
    S1, S2, S3 = "האם AI ממליץ עליך", "מה AI יודע על העסק", "על אילו מקורות AI נשען"
    S4, S5, S6 = "האם AI יכול להיכנס לאתר", "האם AI יכול לקרוא את האתר", "מדידה ופרסום"

    # 1. recommendations
    if not ai.get("engines"):
        out.append(F(S1, "בדיקת המנועים לא רצה", ai.get("skipped", ""), status=NOT_TESTED, level="warn"))
    else:
        rec = ai["recommend"]
        per = []
        for e in ai["engines"]:
            rs = [r for r in rec if r["engine"] == e and not r["error"]]
            errs = [r for r in rec if r["engine"] == e and r["error"]]
            if rs:
                hit = sum(r["mentioned"] for r in rs)
                per.append(f"{L[e]}: הופעת ב-{hit} מתוך {len(rs)} תשובות")
            elif errs:
                per.append(f"{L[e]}: הבדיקה נכשלה ({errs[0]['error'][:80]})")
        ok = [r for r in rec if not r["error"]]
        hits = sum(r["mentioned"] for r in ok)
        comp = _competitors(ai, name, domain).most_common(8)
        comp_items = [f"{n} — הופיע {_times(k)}" for n, k in comp]
        qs = " · ".join(f"„{q}”" for q in ai["questions"])
        zero_engines = [L[e] for e in ai["engines"]
                        if any(r["engine"] == e for r in ok) and not any(r["mentioned"] for r in ok if r["engine"] == e)]
        if ok and hits == 0:
            title, level = "AI לא המליץ עליך באף אחת מהתשובות שבדקנו", "bad"
        elif ok and zero_engines:
            title, level = f"{' ו-'.join(zero_engines)} לא המליץ עליך באף תשובה", "warn"
        elif ok and hits <= len(ok) / 2:
            title, level = f"AI המליץ עליך רק בחלק מהתשובות ({hits} מתוך {len(ok)})", "warn"
        elif ok:
            title, level = f"AI המליץ עליך ברוב התשובות ({hits} מתוך {len(ok)})", "good"
        else:
            title, level = "לא התקבלו תשובות מהמנועים", "warn"
        body = (f"שאלנו את השאלות שלקוח היה שואל: {qs}. כל שאלה הורצה {ai['runs']} פעמים בכל מנוע, "
                f"כי התשובות משתנות בין הרצות.\n" + "\n".join(per))
        if comp_items:
            body += "\n\nבמקומך הופיעו:"
        action = ("כדאי לבדוק מה יש לעסקים שהופיעו ואין לך: ביקורות, אזכורים באתרים שה-AI מצטט (בסעיף הבא) "
                  "ועמודי שירות שמסבירים בבירור מה אתם עושים ואיפה.") if comp_items else ""
        out.append(F(S1, title, body, action, VERIFIED, level, comp_items))

        # 2. knowledge
        for r in ai["knowledge"]:
            if r["error"]:
                out.append(F(S2, f"{L[r['engine']]}: הבדיקה נכשלה", r["error"][:200], status=NOT_TESTED, level="warn"))
                continue
            if r["own_site_cited"]:
                t, lv = f"{L[r['engine']]} מצא את האתר שלך והשתמש בו", "good"
            elif r["mentioned"]:
                t, lv = f"{L[r['engine']]} מכיר את העסק, אבל לא ציטט את האתר שלך", "warn"
            else:
                t, lv = f"{L[r['engine']]} לא מצא מידע על העסק", "bad"
            excerpt = r["text"][:600] + ("…" if len(r["text"]) > 600 else "")
            out.append(F(S2, t, "מה הוא ענה:\n" + excerpt,
                         "קראו את התשובה ובדקו אם הפרטים נכונים (שירותים, אזור, טלפון). מידע שגוי כאן "
                         "יגיע גם ללקוחות ששואלים עליכם.", VERIFIED, lv))

        # 3. sources
        src = _sources(ai, domain).most_common(8)
        if src:
            out.append(F(S3, "האתרים שה-AI מצטט כשהוא עונה על התחום שלך",
                         "אלה המקורות שהופיעו בתשובות על השאלות למעלה:",
                         "להופיע במקורות האלה (פרופיל, ביקורות, כתבה, רשימה) היא הדרך הכי ישירה להיכנס לתשובות.",
                         VERIFIED, "info", [f"{d} — צוטט {_times(k)}" for d, k in src]))

    # 4. access
    robots = site["robots"]
    if robots["error"]:
        out.append(F(S4, "לא הצלחנו לקרוא את robots.txt", robots["error"], status=NOT_TESTED, level="warn"))
    else:
        blocked_search = [b for b in robots["bots"] if b["kind"] == "search" and not b["allowed"]]
        blocked_train = [b for b in robots["bots"] if b["kind"] == "training" and not b["allowed"]]
        if blocked_search:
            out.append(F(S4, "האתר חוסם בוטים שמשפיעים על תשובות AI",
                         "בקובץ robots.txt נמצאה חסימה ל:", "להסיר את החסימה לבוטים האלה, אלא אם היא מכוונת.",
                         VERIFIED, "bad", [f"{b['bot']} ({b['company']}) — {b['purpose']}" for b in blocked_search]))
        else:
            out.append(F(S4, "הבוטים של מנועי ה-AI מורשים להיכנס לאתר",
                         "robots.txt לא חוסם את הבוטים של ChatGPT, Claude, Perplexity, גוגל ו-Bing." if robots["exists"]
                         else "אין באתר robots.txt, ולכן כל הבוטים מורשים.", status=VERIFIED, level="good"))
        if blocked_train:
            out.append(F(S4, "בוטי אימון חסומים", "חסומים: " + ", ".join(b["bot"] for b in blocked_train) +
                         ". זה משפיע על מה שמודלים ילמדו בעתיד, לא על חיפוש היום. זו החלטה עסקית, לא בהכרח תקלה.",
                         status=VERIFIED, level="info"))
    pb = [p for p in site["probe"]["results"] if p["blocked"]]
    if pb:
        out.append(F(S4, "ייתכן שחומת האש של האתר חוסמת בוטים של AI",
                     f"כשנכנסנו כדפדפן רגיל קיבלנו {site['probe']['browser_status']}, וכשנכנסנו בזהות של הבוטים קיבלנו חסימה:",
                     "לבדוק בהגדרות Cloudflare או אצל חברת האחסון אם יש חסימה של בוטי AI.",
                     INFERRED, "warn", [f"{p['bot']}: {p['status'] or 'אין תגובה'}" for p in pb]))

    # 5. readability
    rd = site["readability"]
    if site.get("challenged"):
        out.append(F(S5, "האתר חסם את הסריקה שלנו, ולכן לא בדקנו מה בוטים קוראים בו",
                     f"כשנכנסנו לעמוד הבית בלי דפדפן קיבלנו {site['status']} או דף אימות במקום התוכן. "
                     "ייתכן שגם בוטים של AI מקבלים את אותו דף.",
                     "לבדוק בהגדרות Cloudflare או אצל חברת האחסון אם יש הגנה שחוסמת בקשות אוטומטיות.",
                     NOT_TESTED, "warn"))
    elif rd["ratio"] is not None and rd["rendered_words"] and rd["rendered_words"] > 100 and rd["ratio"] < 0.5:
        out.append(F(S5, "רוב התוכן באתר נטען רק עם JavaScript, ובוטים של AI לא רואים אותו",
                     f"בדפדפן יש בעמוד כ-{rd['rendered_words']} מילים. בגרסה שבוטים קוראים (בלי JavaScript) יש רק כ-{rd['raw_words']}.",
                     "צריך רינדור בצד השרת (SSR) או גרסה סטטית של העמודים החשובים.", VERIFIED, "bad"))
    elif rd["raw_words"] < 80:
        out.append(F(S5, "יש מעט מאוד טקסט בעמוד שהבוטים קוראים",
                     f"נמצאו כ-{rd['raw_words']} מילים ב-HTML.", "להוסיף טקסט שמסביר מה העסק עושה, למי ואיפה.",
                     VERIFIED, "warn"))
    else:
        out.append(F(S5, "התוכן העיקרי קריא גם בלי JavaScript",
                     f"כ-{rd['raw_words']} מילים זמינות לבוטים ישירות." +
                     ("" if site["rendered_ok"] else " (לא הצלחנו להשוות לגרסת הדפדפן.)"),
                     status=VERIFIED if site["rendered_ok"] else INFERRED, level="good"))
    if rd.get("phone_in_rendered") and not rd["phone_in_raw"] and not site.get("challenged"):
        out.append(F(S5, "הטלפון מופיע רק אחרי טעינת JavaScript", "בוטים שלא מריצים JavaScript לא יראו אותו.",
                     "להציג את הטלפון כטקסט רגיל בעמוד.", VERIFIED, "warn"))
    sch = site["schema"]
    bits = ["נמצאו נתונים מובנים שמתארים את העסק" if sch["has_business_schema"] else "אין נתונים מובנים שמתארים את העסק (שם, כתובת, טלפון, שעות)",
            "יש קובץ llms.txt" if site["llms_txt"] else "אין קובץ llms.txt"]
    out.append(F(S5, "סימנים שעוזרים ל-AI להבין את העסק", " · ".join(bits),
                 "" if sch["has_business_schema"] else "להוסיף סימון LocalBusiness עם הפרטים העדכניים. זה לא מבטיח הופעה, אבל מקטין טעויות.",
                 INFERRED, "info"))

    # 6. marketing (short)
    m = site["marketing"]
    have = [k for k, v in m["tags"].items() if v]
    missing = [k for k in ("Google Analytics 4", "פיקסל של מטא (פייסבוק)", "Google Ads (מעקב המרות)") if not m["tags"][k]]
    c = m["contact"]
    contact = [x for x, ok in (("כפתור וואטסאפ", c["whatsapp"]), ("קישור חיוג", c["phone_link"]), ("טופס", c["form"])) if ok]
    body = ("מותקן: " + (", ".join(have) if have else "לא זיהינו כלי מדידה")) + "\n" + \
           ("לא זיהינו: " + ", ".join(missing) if missing else "") + "\n" + \
           ("דרכי פנייה בעמוד: " + (", ".join(contact) if contact else "לא נמצאו"))
    out.append(F(S6, "כלי מדידה ופרסום באתר", body.strip(),
                 "בלי מדידה אי אפשר לדעת מאיפה מגיעים לקוחות ואיזה פרסום מחזיר את הכסף." if missing else "",
                 VERIFIED if m["rendered"] else INFERRED, "warn" if missing else "good"))
    return out


def summarize(report):
    ai = report["ai"]
    ok = [r for r in ai.get("recommend", []) if not r["error"]]
    comp = _competitors(ai, report["business"]["name"], report["business"]["domain"]).most_common(3) if ok else []
    blocked = [b["bot"] for b in report["site"]["robots"]["bots"] if b["kind"] == "search" and not b["allowed"]]
    return {
        "ai_answers": len(ok),
        "ai_mentions": sum(r["mentioned"] for r in ok),
        "top_competitors": [n for n, _ in comp],
        "blocked_search_bots": blocked,
        "tags": [k for k, v in report["site"]["marketing"]["tags"].items() if v],
    }
