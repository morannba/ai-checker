"""End-to-end test with fake websites and fake AI APIs (no internet, no keys needed).

  python tests/e2e_mock.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SITE_SSR = """<!doctype html><html lang="he"><head><title>כהן ושות' רואי חשבון</title>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC1234"></script>
<script src="https://js.hs-scripts.com/123.js"></script>
<meta name="viewport" content="width=device-width"><meta name="description" content="משרד רואי חשבון בחיפה">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"AccountingService","name":"כהן ושות'"}</script>
</head><body><h1>כהן ושות' – רואי חשבון בחיפה</h1>
<p>משרד רואי חשבון בחיפה. דוחות שנתיים, הנהלת חשבונות ומיסוי לעצמאים ולחברות קטנות. """ + "מילה " * 200 + """</p>
<a href="https://wa.me/972501234567">וואטסאפ</a> <a href="tel:04-8123456">04-8123456</a></body></html>"""

SITE_JS = """<!doctype html><html><head><title>Loading</title>
<script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-XYZ123');</script></head>
<body><div id="root"></div><script>
document.getElementById('root').innerHTML = '<h1>לוי מרפאת שיניים</h1><p>' + 'טיפולי שיניים ברמה גבוהה '.repeat(80) + '</p><p>052-7654321</p>';
</script></body></html>"""

ROBOTS_BLOCK = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: OAI-SearchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        ua = self.headers.get("User-Agent", "")
        if self.path == "/robots.txt":
            body, ctype, code = (ROBOTS_BLOCK if port == SSR_PORT else ""), "text/plain", (200 if port == SSR_PORT else 404)
        elif self.path == "/llms.txt":
            body, ctype, code = "<html>not found page</html>", "text/html", 200  # soft-404 trap
        else:
            if port == SSR_PORT and "PerplexityBot" in ua:  # simulate a firewall block
                body, ctype, code = "Forbidden", "text/html", 403
            else:
                body, ctype, code = (SITE_SSR if port == SSR_PORT else SITE_JS), "text/html; charset=utf-8", 200
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ANSWER = ("הנה כמה משרדים מומלצים בחיפה: 1. משרד לוינסון רואי חשבון – ותק רב. "
          "2. ברק את גולן – שירות אישי. 3. מזרחי ושות'. כדאי לבדוק ביקורות.")
ANSWER_WITH_US = ANSWER + " 4. כהן ושות' רואי חשבון."


class AI(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        AI.calls.append((self.path, body))
        if self.path.endswith("/responses"):  # OpenAI
            q = body["input"]
            if "verdict" in q:
                out = {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"verdict": "none"}'}]}]}
            elif "JSON" in q:
                out = {"output": [{"type": "message", "content": [{"type": "output_text",
                       "text": json.dumps({"businesses": ["משרד לוינסון רואי חשבון", "ברק את גולן", "מזרחי ושות'"]}, ensure_ascii=False)}]}]}
            else:
                text = "כהן ושות' הוא משרד רואי חשבון בחיפה." if "מה אתה יודע" in q else ANSWER
                out = {"output": [{"type": "web_search_call"}, {"type": "message", "content": [{"type": "output_text", "text": text,
                       "annotations": [{"type": "url_citation", "url": "https://www.d.co.il/x", "title": "דפי זהב"},
                                       {"type": "url_citation", "url": "https://www.easy.co.il/y", "title": "איזי"}]}]}]}
        elif ":generateContent" in self.path:  # Gemini
            q = body["contents"][0]["parts"][0]["text"]
            if "verdict" in q:
                text = '{"verdict": "match"}'
            elif "JSON" in q:
                text = json.dumps({"businesses": ["ברק את גולן", "כהן ושות' רואי חשבון"]}, ensure_ascii=False)
            else:
                text = ANSWER_WITH_US
            out = {"candidates": [{"content": {"parts": [{"text": text}]},
                   "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://vertexaisearch/redirect/1", "title": "d.co.il"}}]}}]}
        else:  # Anthropic
            out = {"content": [{"type": "server_tool_use"}, {"type": "text", "text": "x"}]}
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"simulated outage"}')
            return
        data = json.dumps(out, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s.server_address[1]


if __name__ == "__main__":
    SSR_PORT = serve(Site)
    JS_PORT = serve(Site)
    AI_PORT = serve(AI)
    os.environ.update({
        "ALLOW_PRIVATE_HOSTS": "1", "CHROMIUM_PATH": "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
        "OPENAI_API_KEY": "test", "OPENAI_BASE_URL": f"http://127.0.0.1:{AI_PORT}/v1",
        "GEMINI_API_KEY": "test", "GEMINI_BASE_URL": f"http://127.0.0.1:{AI_PORT}/v1beta",
        "ANTHROPIC_API_KEY": "test", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{AI_PORT}",
        "DATA_DIR": "/tmp/ai_checker_test_data", "PUBLIC_ENGINES": "chatgpt",
    })
    from checker.audit import audit

    r1 = audit(f"http://127.0.0.1:{SSR_PORT}/", "כהן ושות'", "רואה חשבון", "חיפה", runs=2)
    print("== SSR site, all engines ==")
    print(json.dumps(r1["summary"], ensure_ascii=False))
    for f in r1["findings"]:
        print(f"[{f['level']:4}] {f['section']} | {f['title']}")
        for it in f["items"]:
            print("        -", it)

    r2 = audit(f"http://127.0.0.1:{JS_PORT}/", "לוי מרפאת שיניים", "מרפאת שיניים", "", engines=["chatgpt"], runs=1)
    print("\n== JS-only site, ChatGPT only ==")
    for f in r2["findings"]:
        print(f"[{f['level']:4}] {f['section']} | {f['title']}")

    # Web app flow
    import app as webapp
    c = webapp.app.test_client()
    print("\nGET / ->", c.get("/").status_code)
    bad = c.post("/api/audit", json={"url": "x"})
    print("missing fields ->", bad.status_code, bad.get_json()["step"], bad.get_json()["error"])
    site = f"http://127.0.0.1:{SSR_PORT}/"
    step1 = {"url": site, "business_name": "כהן ושות'", "business_type": "רואה חשבון", "city": "חיפה"}
    nophone = c.post("/api/audit", json=dict(step1, name="דנה"))
    print("no phone ->", nophone.status_code, nophone.get_json()["step"], nophone.get_json()["error"])
    badmail = c.post("/api/audit", json=dict(step1, name="דנה", phone="050-1234567", email="nope"))
    print("bad optional email ->", badmail.status_code, badmail.get_json()["error"])
    print("site name ->", c.get("/api/site-name?url=" + site).get_json())
    print("site name, private host blocked ->", (os.environ.pop("ALLOW_PRIVATE_HOSTS"),
          c.get("/api/site-name?url=http://127.0.0.1/").get_json(), os.environ.update(ALLOW_PRIVATE_HOSTS="1"))[1])
    key = "ab" * 12
    print("extra (before) ->", c.post("/api/extra", json={"lead_key": key, "ad_budget": "עד ₪2,000", "ad_platforms": ["גוגל"]}).status_code)
    ok = c.post("/api/audit", json=dict(step1, name="דנה", phone="050-1234567", lead_key=key)).get_json()
    print("audit, no email ->", ok["ok"], "engines:", ok["report"]["ai"]["engines"])
    print("extra (late) ->", c.post("/api/extra", json={"lead_key": key, "ad_budget": "עד ₪2,000", "goal": "יותר לקוחות"}).status_code)
    print("extra, bad key ->", c.post("/api/extra", json={"lead_key": "x"}).status_code)
    print("saved report page ->", c.get("/r/" + ok["report"]["id"]).status_code)
    print("leads.csv:\n" + open("/tmp/ai_checker_test_data/leads.csv", encoding="utf-8-sig").read())
    for _ in range(3):
        last = c.post("/api/audit", json={"url": site, "business_name": "a", "business_type": "b",
                                           "name": "x", "phone": "0521111111"})
    print("rate limit ->", last.status_code)
    print("AI calls made:", len(AI.calls))
