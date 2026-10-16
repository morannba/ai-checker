"""The report findings in English, for the English page. Same checks and logic as build_findings in audit.py;
a change to one should be made in the other too."""
from .ai_engines import ENGINE_LABELS as L

VERIFIED, INFERRED, NOT_TESTED = "Checked", "Estimate", "Not checked"

# site_signals keeps Hebrew keys for the stack; these are their English names
GROUP_EN = {"פרסום": "Ads", "מדידה": "Measurement", "CRM ואוטומציה": "CRM & automation"}
TOOL_EN = {"פיקסל של מטא (פייסבוק)": "Meta pixel (Facebook)", "Google Ads (מעקב המרות)": "Google Ads (conversion tracking)",
           "פיקסל של טיקטוק": "TikTok pixel"}


def F(section, title, body, action="", status=VERIFIED, level="info", items=None):
    return {"section": section, "title": title, "body": body, "action": action, "status": status,
            "level": level, "items": items or []}


def _and(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def build_findings(name, domain, site, ai):
    out = []
    S1, S2 = "Does AI recommend you?", "What AI knows about the business"
    S4, S5, S6 = "Can AI get into the site?", "Can AI read the site?", "The business's digital setup"

    # 1. recommendations
    if not ai.get("engines"):
        out.append(F(S1, "The AI check didn't run", ai.get("skipped", ""), status=NOT_TESTED, level="warn"))
    else:
        rec = ai["recommend"]
        per = []
        for e in ai["engines"]:
            rs = [r for r in rec if r["engine"] == e and not r["error"]]
            errs = [r for r in rec if r["engine"] == e and r["error"]]
            if rs:
                hit = sum(r["mentioned"] for r in rs)
                per.append(f"{L[e]}: you appeared in {hit} of {len(rs)} answers")
            elif errs:
                per.append(f"{L[e]}: the check failed ({errs[0]['error'][:80]})")
        ok = [r for r in rec if not r["error"]]
        hits = sum(r["mentioned"] for r in ok)
        qs = " · ".join(f"“{q}”" for q in ai["questions"])
        zero_engines = [L[e] for e in ai["engines"]
                        if any(r["engine"] == e for r in ok) and not any(r["mentioned"] for r in ok if r["engine"] == e)]
        if ok and hits == 0:
            title, level = "AI didn't recommend you in any of the answers we checked", "bad"
        elif ok and zero_engines:
            title, level = f"{_and(zero_engines)} didn't recommend you in any answer", "warn"
        elif ok and hits <= len(ok) / 2:
            title, level = f"AI recommended you in only some of the answers ({hits} of {len(ok)})", "warn"
        elif ok:
            title, level = f"AI recommended you in most of the answers ({hits} of {len(ok)})", "good"
        else:
            title, level = "No answers came back from the engines", "warn"
        body = (f"We asked the questions a customer would ask: {qs}. Each question ran {ai['runs']} times in each "
                f"engine, because the answers change from run to run.\n" + "\n".join(per))
        action = ("To show up in more answers: get featured on sites that cover your field (guides, directories, "
                  "articles), collect reviews, and make sure your site has service pages that clearly explain what "
                  "you do and for whom.") if ok and hits < len(ok) else ""
        out.append(F(S1, title, body, action, VERIFIED, level))

        # 2. knowledge
        for r in ai["knowledge"]:
            if r["error"]:
                out.append(F(S2, f"{L[r['engine']]}: the check failed", r["error"][:200], status=NOT_TESTED, level="warn"))
                continue
            excerpt = r["text"][:600] + ("…" if len(r["text"]) > 600 else "")
            if r.get("identity") == "other":
                out.append(F(S2, f"{L[r['engine']]} confuses you with another business or person",
                             "The description it gave doesn't match what your website says:\n" + excerpt,
                             "When someone else has a similar name, AI mixes you up. Strengthen your identity online: "
                             "your full name, who is behind the business and what it does, consistently on your site, "
                             "your profiles and your structured data (Schema), so the engine knows who is who.",
                             VERIFIED, "bad"))
                continue
            if r["own_site_cited"]:
                t, lv = f"{L[r['engine']]} found your website and used it", "good"
            elif r["mentioned"]:
                t, lv = f"{L[r['engine']]} knows the business, but didn't cite your website", "warn"
            else:
                t, lv = f"{L[r['engine']]} found no information about the business", "bad"
            out.append(F(S2, t, "What it answered:\n" + excerpt,
                         "Read the answer and check the details are right (services, area, phone). Wrong information "
                         "here also reaches customers who ask about you.", VERIFIED, lv))

    # 4. access
    robots = site["robots"]
    if robots["error"]:
        out.append(F(S4, "We couldn't read robots.txt", robots["error"], status=NOT_TESTED, level="warn"))
    else:
        blocked_search = [b for b in robots["bots"] if b["kind"] == "search" and not b["allowed"]]
        blocked_train = [b for b in robots["bots"] if b["kind"] == "training" and not b["allowed"]]
        if blocked_search:
            out.append(F(S4, "The site blocks bots that affect AI answers", "robots.txt blocks:",
                         "Remove the block for these bots, unless it is intended.", VERIFIED, "bad",
                         [f"{b['bot']} ({b['company']}) — {b.get('purpose_en') or b['purpose']}" for b in blocked_search]))
        else:
            out.append(F(S4, "AI engines' bots are allowed into the site",
                         "robots.txt doesn't block the bots of ChatGPT, Claude, Perplexity, Google and Bing."
                         if robots["exists"] else "The site has no robots.txt, so all bots are allowed.",
                         status=VERIFIED, level="good"))
        if blocked_train:
            out.append(F(S4, "Training bots are blocked", "Blocked: " + ", ".join(b["bot"] for b in blocked_train) +
                         ". This affects what future models learn, not search today. It's a business decision, "
                         "not necessarily a problem.", status=VERIFIED, level="info"))
    pb = [p for p in site["probe"]["results"] if p["blocked"]]
    if pb:
        out.append(F(S4, "The site's firewall may be blocking AI bots",
                     f"As a normal browser we got {site['probe']['browser_status']}, and as the bots we were blocked:",
                     "Check your Cloudflare settings, or ask your hosting company, whether AI bots are blocked.",
                     INFERRED, "warn", [f"{p['bot']}: {p['status'] or 'no response'}" for p in pb]))

    # 5. readability
    rd = site["readability"]
    if site.get("challenged"):
        out.append(F(S5, "The site blocked our scan, so we didn't check what bots can read",
                     f"Opening the homepage without a browser we got {site['status']} or a verification page "
                     "instead of the content. AI bots may get the same page.",
                     "Check your Cloudflare settings, or ask your hosting company, whether a protection blocks "
                     "automated requests.", NOT_TESTED, "warn"))
    elif rd["ratio"] is not None and rd["rendered_words"] and rd["rendered_words"] > 100 and rd["ratio"] < 0.5:
        out.append(F(S5, "Most of the site's content loads only with JavaScript, and AI bots don't see it",
                     f"In a browser the page has about {rd['rendered_words']} words. The version bots read "
                     f"(without JavaScript) has only about {rd['raw_words']}.",
                     "You need server-side rendering (SSR) or a static version of the important pages.", VERIFIED, "bad"))
    elif rd["raw_words"] < 80:
        out.append(F(S5, "There is very little text on the page bots read",
                     f"We found about {rd['raw_words']} words in the HTML.",
                     "Add text that explains what the business does, for whom and where.", VERIFIED, "warn"))
    else:
        out.append(F(S5, "The main content is readable without JavaScript",
                     f"About {rd['raw_words']} words are available to bots directly." +
                     ("" if site["rendered_ok"] else " (We couldn't compare it with the browser version.)"),
                     status=VERIFIED if site["rendered_ok"] else INFERRED, level="good"))
    if rd.get("phone_in_rendered") and not rd["phone_in_raw"] and not site.get("challenged"):
        out.append(F(S5, "The phone number appears only after JavaScript loads", "Bots that don't run JavaScript won't see it.",
                     "Show the phone number as plain text on the page.", VERIFIED, "warn"))
    sch = site["schema"]
    bits = ["Structured data describing the business was found" if sch["has_business_schema"]
            else "No structured data describing the business (name, address, phone, hours)",
            "There is an llms.txt file" if site["llms_txt"] else "No llms.txt file"]
    out.append(F(S5, "Signals that help AI understand the business", " · ".join(bits),
                 "" if sch["has_business_schema"] else "Add LocalBusiness markup with up-to-date details. It doesn't "
                 "guarantee you'll appear, but it reduces mistakes.", INFERRED, "info"))

    # 6. digital stack
    out += stack_findings(S6, site)
    return out


def stack_findings(section, site):
    m, out = site["marketing"], []
    tags, stack = m["tags"], m["stack"]
    seen = VERIFIED if m["rendered"] else INFERRED
    c = m["contact"]
    contact = [x for x, ok in (("WhatsApp button", c["whatsapp"]), ("click-to-call link", c["phone_link"]),
                               ("form", c["form"]), ("chat", c["chat"])) if ok]
    body = ("The site is built on " + " / ".join(m["platform"]) + ". " if m["platform"] else "") + \
           "Ways to get in touch on the homepage: " + (", ".join(contact) if contact else "none found")
    grid = []
    for g, tools in stack.items():
        rows = [{"name": TOOL_EN.get(n, n), "ok": ok} for n, ok in tools.items()]
        if g == "CRM ואוטומציה":  # a long column of ✗ says nothing: show what was found, or one line
            rows = [r for r in rows if r["ok"]] or [{"name": "No CRM or automation tool detected", "ok": False}]
        grid.append({"group": GROUP_EN.get(g, g), "rows": rows})
    f = F(section, "What's installed on the site", body, status=seen, level="info")
    f["grid"] = grid
    out.append(f)

    ads = [TOOL_EN.get(n, n) for n, ok in stack["פרסום"].items() if ok]
    crm = [n for n, ok in stack["CRM ואוטומציה"].items() if ok]
    ga4, gtm = tags["Google Analytics 4"], tags["Google Tag Manager"]
    if ads and not ga4:
        out.append(F(section, "There are ad pixels, but no Google Analytics",
                     "We detected " + ", ".join(ads) + ", but not Google Analytics 4. Each platform reports on itself, "
                     "and there's no single place that shows where customers really come from.",
                     "Install GA4 (ideally through Tag Manager) and set up your enquiries as conversions.", seen, "bad"))
    elif ga4 and not ads:
        out.append(F(section, "There's measurement, but no ad pixel",
                     "Google Analytics is installed, but we didn't detect a Meta or Google Ads pixel. Without a pixel "
                     "you can't re-reach people who already visited the site (remarketing), and the ad platforms "
                     "can't learn who becomes a lead.",
                     "Install the Meta pixel and the Google Ads tag before you start advertising.", seen, "warn"))
    elif ga4 and ads:
        pass  # both measurement and ad pixels: nothing to flag here
    elif gtm:
        out.append(F(section, "There's Tag Manager, but we didn't see Analytics or an ad pixel through it",
                     "The setup is there, but we didn't see it fire Google Analytics 4, a Meta pixel or a Google Ads tag.",
                     "Check what's configured in Tag Manager and add GA4 and your ad pixels there.", seen, "warn"))
    else:
        out.append(F(section, "We didn't detect any measurement or ad tools on the site",
                     "Without measurement there's no way to know how many people visit the site, where they come "
                     "from and what brings enquiries.",
                     "Start with Google Analytics 4 and Tag Manager. Both are free.", seen, "bad"))
    if (ga4 or ads) and not gtm:
        out.append(F(section, "The tags are installed without Tag Manager",
                     "Every change to measurement (a new event, a new pixel) means going into the site's code.",
                     "Put all your tags in Google Tag Manager, so you can change measurement without a developer.",
                     seen, "info"))
    if crm:
        out.append(F(section, "The site is connected to a CRM or automation tool",
                     "We detected: " + ", ".join(crm) + ". Make sure every enquiry reaches it with its source "
                     "(Google, Facebook, etc.), so you know which channel brings customers and not just leads.",
                     status=seen, level="good"))
    else:
        out.append(F(section, "We didn't detect a CRM connection",
                     "We didn't see a form or code from a CRM or automation system on the homepage (HubSpot, "
                     "Salesforce, Pipedrive, Zoho and others). There may be a server-side connection we can't see "
                     "from outside.",
                     "If enquiries only go to email or WhatsApp, they're easy to lose and you can't tell which "
                     "channel brought a customer. Connecting the form to a CRM solves both.", INFERRED, "warn"))

    seo = site.get("seo")
    if seo and not site.get("challenged"):
        miss = []
        if not seo["title"]:
            miss.append("The page has no title")
        elif len(seo["title"]) < 15:
            miss.append(f"The title is very short: “{seo['title']}”")
        if not seo["description"]:
            miss.append("The page has no meta description, so Google picks the text itself")
        if seo["h1"] == 0:
            miss.append("The page has no main heading (H1)")
        elif seo["h1"] > 1:
            miss.append(f"There are {seo['h1']} main headings (H1) instead of one")
        if not seo["viewport"]:
            miss.append("The page isn't set up for mobile (viewport)")
        if not seo["sitemap"]:
            miss.append("We didn't find a sitemap.xml")
        if miss:
            out.append(F(section, "SEO basics on the homepage: things to fix",
                         "We checked the basics that Google and AI engines rely on:",
                         "These are small fixes that affect how the site appears in Google and in AI answers.",
                         VERIFIED, "warn", miss))
        else:
            out.append(F(section, "The SEO basics on the homepage are in place",
                         "There's a title, a description, one main heading, mobile setup and a sitemap.",
                         status=VERIFIED, level="good"))
    return out
