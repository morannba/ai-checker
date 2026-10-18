"""Runs all checks and turns them into a short, plain-language report (no overall score), in Hebrew or English."""
import datetime
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup

from . import ai_access, ai_engines, render, report_en, site_signals
from .fetch import decode, normalize_url, safe_get

VERIFIED, INFERRED, NOT_TESTED = "נבדק בפועל", "הערכה", "לא נבדק"
STATUS_EN = {VERIFIED: "Checked", INFERRED: "Estimate", NOT_TESTED: "Not checked"}
LANGS = ("he", "en")


SCOPES = ("local", "israel", "global")  # where the business's customers come from


# ccTLDs that are mostly used as generic domains, so they say nothing about where the business is
_GENERIC_CC = {"io", "ai", "co", "me", "tv", "ly", "gg", "cc", "fm", "to", "so", "sh", "ws", "is", "eu", "app"}
COUNTRY_EN = {"IL": "Israel", "GB": "the UK", "IE": "Ireland", "DE": "Germany", "NL": "the Netherlands",
              "FR": "France", "ES": "Spain", "IT": "Italy", "BE": "Belgium", "AT": "Austria", "CH": "Switzerland",
              "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland", "PT": "Portugal", "PL": "Poland",
              "CZ": "the Czech Republic", "GR": "Greece", "US": "the US", "CA": "Canada", "AU": "Australia",
              "NZ": "New Zealand"}


def country_from_domain(domain):
    """ISO country code guessed from the domain ending (example.co.uk -> GB), or None for .com and the like."""
    tld = (domain or "").lower().rsplit(".", 1)[-1]
    if len(tld) != 2 or tld in _GENERIC_CC:
        return None
    return "GB" if tld == "uk" else tld.upper()


def build_questions_en(business_type, city, scope, country):
    if scope == "local" and city:
        where = f" in {city}"
    elif scope == "israel" and country in COUNTRY_EN:  # "israel" = the whole country on the English page
        where = f" in {COUNTRY_EN[country]}"
    else:
        where = ""
    t = business_type.strip()
    # phrased to work for a profession ("accountant") and a service ("web design") alike
    plain = re.match(r"(a|an|the)\s", t, re.I) or t.lower().endswith(("s", "services", "design", "marketing"))
    article = "" if plain else ("an " if t[:1].lower() in "aeiou" else "a ")
    return [
        f"I'm looking for {article}{t}{where}. Who should I check out?",
        f"What are the best {t} options{where}?",
        f"Can you name a few recommended {t} options{where}, and why those?",
    ]


def build_questions(business_type, city, custom=None, scope="local", lang="he", country=None):
    if custom:
        return [q.strip() for q in custom if q.strip()][:5]
    city = city.strip()
    if lang == "en":
        return build_questions_en(business_type, city, scope, country)
    if scope == "global":
        where = ""  # online / B2B: customers search without a place
    elif scope == "local" and city:
        # "בחיפה", but "ב-Haifa": a Hebrew prefix glued to Latin letters reads as a typo
        where = " " + (f"ב{city}" if re.match(r"[\u0590-\u05ff]", city) else f"ב-{city}")
    else:
        where = " בישראל"
    t = business_type.strip()
    # phrased to read naturally for a profession ("רואה חשבון") and a service ("בניית אתרים") alike
    return [
        f"אני מחפש {t}{where}. את מי כדאי לי לבדוק?",
        f"מה ההמלצות הכי טובות ל{t}{where}" + (" והסביבה?" if scope == "local" else "?"),
        f"תן לי כמה שמות מומלצים ל{t}{where}, ולמה דווקא הם.",
    ]


def resolve_scope(scope, city):
    scope = scope if scope in SCOPES else ("local" if city.strip() else "israel")
    return "israel" if scope == "local" and not city.strip() else scope


def knowledge_question(name, domain, lang="he"):
    # anchored to the site: a shared name must not pull in another business or person
    if lang == "en":
        return (f"What do you know about the business whose website is {domain} (business name: {name})? "
                f"Answer only about the business of this website, and don't mix in information about other "
                f"businesses or people with a similar name. What do they do, where do they operate and what do "
                f"people say about them?")
    return (f"מה אתה יודע על העסק שהאתר שלו הוא {domain} (שם העסק: {name})? "
            f"ענה רק על העסק של האתר הזה, ואל תערבב מידע על עסקים או אנשים אחרים עם שם דומה. "
            f"מה הם עושים, איפה הם פועלים ומה אומרים עליהם?")


def site_summary(html):
    """What the homepage says about the business, so the judge can tell it apart from a namesake."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = meta.get("content", "") if meta else ""
    return "\n".join(x for x in (title, desc, site_signals.visible_text_from_html(html)[:1500]) if x)


def run_ai(name, domain, business_type, city, custom_questions=None, engines=None, runs=None, scope="local",
           site_text="", lang="he"):
    available = ai_engines.enabled_engines()
    engines = [e for e in (engines or available) if e in available]
    if not engines:
        return {"engines": [], "skipped": "No API key is set for any AI engine" if lang == "en"
                else "לא הוגדר מפתח API לאף מנוע AI"}
    runs = runs or int(os.environ.get("AI_RUNS", "2"))
    # Hebrew page: customers are in Israel. English page: the country comes from the domain ending, if any.
    country = country_from_domain(domain) if lang == "en" else "IL"
    questions = build_questions(business_type, city, custom_questions, scope, lang, country)
    # where the search pretends the customer is: their city, anywhere in the country, or nowhere in particular
    location = city if scope == "local" else ("" if scope == "israel" else None)
    if location == "" and not country:
        location = None  # "my country" on a .com site: no country to point the search at
    jobs = []  # (kind, engine, question)
    for e in engines:
        jobs.append(("knowledge", e, knowledge_question(name, domain, lang)))
        for q in questions:
            jobs += [("recommend", e, q)] * runs
    workers = int(os.environ.get("AI_CONCURRENCY", "6"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda j: (j[0], ai_engines.ask(j[1], j[2], location, country=country)), jobs))

    rec = [r for k, r in results if k == "recommend"]
    for r in rec:
        r["mentioned"], r["own_site_cited"] = ai_engines.is_mentioned(r, name, domain)
    know = [r for k, r in results if k == "knowledge"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        verdicts = list(pool.map(
            lambda r: ai_engines.knows_business(r["text"], r["question"], r["engine"], domain, site_text), know))
    for r, v in zip(know, verdicts):
        _, r["own_site_cited"] = ai_engines.is_mentioned(r, name, domain)
        r["identity"] = v  # "match" / "other" (describes a namesake) / "none"
        r["mentioned"] = v == "match" or (v != "other" and r["own_site_cited"])
    return {"engines": engines, "questions": questions, "runs": runs, "recommend": rec, "knowledge": know}


def fetch_home(url, lang="he"):
    page = safe_get(url)
    if page["error"] or page["status"] is None:
        raise ValueError("We couldn't reach the site. Check that the address is right and the site is up."
                         if lang == "en" else "לא הצלחנו להגיע לאתר. בדקו שהכתובת נכונה ושהאתר פעיל.")
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
        "seo": site_signals.seo_basics(raw_html, final_url),
        "rendered_ok": bool(rendered.get("ok")),
    }


def audit(url, business_name, business_type, city="", custom_questions=None, engines=None, runs=None, scope=None,
          lang="he"):
    lang = lang if lang in LANGS else "he"
    scope = resolve_scope(scope, city)
    if scope != "local":
        city = ""
    url = normalize_url(url)
    domain = urllib.parse.urlparse(url).hostname.removeprefix("www.")
    page = fetch_home(url, lang)  # before any paid AI call: a typo in the URL should cost nothing
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_site = pool.submit(run_site, page, business_name)
        f_ai = pool.submit(run_ai, business_name, domain, business_type, city, custom_questions, engines, runs, scope,
                           site_summary(decode(page["content"])), lang)
        site, ai = f_site.result(), f_ai.result()
    report = {
        "business": {"name": business_name, "type": business_type, "city": city, "scope": scope, "url": url, "domain": domain},
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="minutes"),
        "lang": lang,
        "findings": build_findings(business_name, domain, site, ai, lang),
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


def build_findings(name, domain, site, ai, lang="he"):
    if lang == "en":
        return report_en.build_findings(name, domain, site, ai)
    out = []
    S1, S2 = "האם AI ממליץ עליך", "מה AI יודע על העסק"
    S4, S5, S6 = "האם AI יכול להיכנס לאתר", "האם AI יכול לקרוא את האתר", "התמונה הדיגיטלית של העסק"

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
        # No list of "competitors": the names in an answer are not necessarily competing with this business.
        action = ("כדי להופיע ביותר תשובות: להופיע באתרים שמדברים על התחום שלכם (מדריכים, אינדקסים, כתבות), לאסוף ביקורות, "
                  "ולוודא שבאתר יש עמודי שירות שמסבירים בבירור מה אתם עושים ולמי.") if ok and hits < len(ok) else ""
        out.append(F(S1, title, body, action, VERIFIED, level))

        # 2. knowledge
        for r in ai["knowledge"]:
            if r["error"]:
                out.append(F(S2, f"{L[r['engine']]}: הבדיקה נכשלה", r["error"][:200], status=NOT_TESTED, level="warn"))
                continue
            if r.get("identity") == "other":
                t, lv = f"{L[r['engine']]} מבלבל אותך עם עסק או אדם אחר", "bad"
                excerpt = r["text"][:600] + ("…" if len(r["text"]) > 600 else "")
                out.append(F(S2, t, "התיאור שהוא נתן לא מתאים למה שכתוב באתר שלך:\n" + excerpt,
                             "כשיש עוד גורם עם שם דומה, ה-AI מערבב ביניכם. צריך לחזק את הזהות שלכם ברשת: "
                             "שם מלא, מי עומד מאחורי העסק ומה הוא עושה, באופן עקבי באתר, בפרופילים ובנתונים "
                             "המובנים (Schema), כדי שהמנוע יידע מי זה מי.", VERIFIED, lv))
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

    # 6. digital stack: what is installed, and what it says about how the business markets itself
    out += stack_findings(S6, site)
    return out


def stack_findings(section, site):
    m, out = site["marketing"], []
    tags, stack = m["tags"], m["stack"]
    seen = VERIFIED if m["rendered"] else INFERRED
    c = m["contact"]
    contact = [x for x, ok in (("כפתור וואטסאפ", c["whatsapp"]), ("קישור חיוג", c["phone_link"]), ("טופס", c["form"]),
                               ("צ'אט", c["chat"])) if ok]
    body = ("האתר בנוי על " + " / ".join(m["platform"]) + ". " if m["platform"] else "") + \
           "דרכי פנייה בעמוד הבית: " + (", ".join(contact) if contact else "לא נמצאו")
    grid = []
    for g, tools in stack.items():
        rows = [{"name": n, "ok": ok} for n, ok in tools.items()]
        if g == "CRM ואוטומציה":  # a long column of ✗ says nothing: show what was found, or one line
            rows = [r for r in rows if r["ok"]] or [{"name": "לא זוהה CRM או כלי אוטומציה", "ok": False}]
        grid.append({"group": g, "rows": rows})
    f = F(section, "מה מותקן באתר", body, status=seen, level="info")
    f["grid"] = grid
    out.append(f)

    ads = [n for n, ok in stack["פרסום"].items() if ok]
    crm = [n for n, ok in stack["CRM ואוטומציה"].items() if ok]
    ga4, gtm = tags["Google Analytics 4"], tags["Google Tag Manager"]
    if ads and not ga4:
        out.append(F(section, "יש פיקסלים של פרסום, אבל אין Google Analytics",
                     "זיהינו " + ", ".join(ads) + ", אבל לא Google Analytics 4. "
                     "כל פלטפורמה מדווחת על עצמה, ואין מקום אחד שמראה מאיפה באמת מגיעים הלקוחות.",
                     "להתקין GA4 (עדיף דרך Tag Manager) ולהגדיר בו את הפניות כהמרות.", seen, "bad"))
    elif ga4 and not ads:
        out.append(F(section, "יש מדידה, אבל אין פיקסל פרסום",
                     "Google Analytics מותקן, אבל לא זיהינו פיקסל של מטא או של Google Ads. "
                     "בלי פיקסל אי אפשר להחזיר בפרסום את מי שכבר ביקר באתר (רימרקטינג), "
                     "ואי אפשר לתת למערכות הפרסום ללמוד מי הופך לליד.",
                     "להתקין את הפיקסל של מטא ואת תג Google Ads עוד לפני שמתחילים לפרסם.", seen, "warn"))
    elif ga4 and ads:
        pass  # both measurement and ad pixels: nothing to flag here
    elif gtm:
        out.append(F(section, "יש Tag Manager, אבל לא זיהינו דרכו Analytics או פיקסל פרסום",
                     "התשתית קיימת, אבל לא ראינו שהיא מפעילה Google Analytics 4, פיקסל של מטא או תג של Google Ads.",
                     "לבדוק מה מוגדר ב-Tag Manager ולהוסיף בו GA4 ואת הפיקסלים של הפרסום.", seen, "warn"))
    else:
        out.append(F(section, "לא זיהינו באתר כלי מדידה או פרסום",
                     "בלי מדידה אין דרך לדעת כמה אנשים נכנסים לאתר, מאיפה הם מגיעים ומה מביא פניות.",
                     "להתחיל ב-Google Analytics 4 ו-Tag Manager. שניהם בחינם.", seen, "bad"))
    if (ga4 or ads) and not gtm:
        out.append(F(section, "התגים מותקנים בלי Tag Manager",
                     "כל שינוי במדידה (אירוע חדש, פיקסל חדש) דורש כניסה לקוד של האתר.",
                     "לרכז את כל התגים ב-Google Tag Manager, כדי לשנות מדידה בלי מתכנת.", seen, "info"))
    if crm:
        out.append(F(section, "האתר מחובר ל-CRM או לכלי אוטומציה",
                     "זיהינו: " + ", ".join(crm) + ". כדאי לוודא שכל פנייה נכנסת אליו עם המקור שלה "
                     "(גוגל, פייסבוק וכו'), כדי לדעת איזה ערוץ מביא לקוחות ולא רק לידים.", status=seen, level="good"))
    else:
        out.append(F(section, "לא זיהינו חיבור ל-CRM",
                     "לא ראינו בעמוד הבית טופס או קוד של מערכת CRM או אוטומציה (HubSpot, monday, Fireberry, Zoho ועוד). "
                     "ייתכן שיש חיבור בצד השרת שלא רואים מבחוץ.",
                     "אם הפניות מגיעות רק למייל או לוואטסאפ, קל לאבד אותן ואי אפשר לדעת איזה ערוץ הביא לקוח. "
                     "חיבור הטופס ל-CRM פותר את שני הדברים.", INFERRED, "warn"))

    seo = site.get("seo")
    if seo and not site.get("challenged"):
        miss = []
        if not seo["title"]:
            miss.append("אין כותרת לעמוד (title)")
        elif len(seo["title"]) < 15:
            miss.append(f"הכותרת קצרה מאוד: „{seo['title']}”")
        if not seo["description"]:
            miss.append("אין תיאור לעמוד (meta description), אז גוגל יבחר טקסט בעצמו")
        if seo["h1"] == 0:
            miss.append("אין כותרת ראשית (H1) בעמוד")
        elif seo["h1"] > 1:
            miss.append(f"יש {seo['h1']} כותרות ראשיות (H1) במקום אחת")
        if not seo["viewport"]:
            miss.append("העמוד לא מוגדר לנייד (viewport)")
        if not seo["sitemap"]:
            miss.append("לא מצאנו sitemap.xml")
        if miss:
            out.append(F(section, "יסודות SEO בעמוד הבית: יש מה לתקן", "בדקנו את הבסיס שגוגל ומנועי AI מסתמכים עליו:",
                         "אלה תיקונים קטנים שמשפיעים על איך האתר מופיע בגוגל ובתשובות AI.", VERIFIED,
                         "warn", miss))
        else:
            out.append(F(section, "יסודות ה-SEO בעמוד הבית במקום",
                         "יש כותרת, תיאור, כותרת ראשית אחת, התאמה לנייד ו-sitemap.", status=VERIFIED, level="good"))
    return out


def summarize(report):
    ai = report["ai"]
    ok = [r for r in ai.get("recommend", []) if not r["error"]]
    blocked = [b["bot"] for b in report["site"]["robots"]["bots"] if b["kind"] == "search" and not b["allowed"]]
    return {
        "ai_answers": len(ok),
        "ai_mentions": sum(r["mentioned"] for r in ok),
        "blocked_search_bots": blocked,
        "tags": [k for k, v in report["site"]["marketing"]["tags"].items() if v],
        # an ad pixel on the site means the business already pays for ads: the leads worth chasing
        "advertises": any(report["site"]["marketing"]["stack"]["פרסום"].values()),
    }
