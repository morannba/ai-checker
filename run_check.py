"""Run a full check from the command line (all engines with keys, not just the public ones).

Usage:
  python run_check.py <url> "<business name>" "<business type>" "<city>" [--engines chatgpt,gemini,claude] [--runs 3]

Saves the report to data/reports/<id>.json. With the app running, open http://127.0.0.1:5000/r/<id>
"""
import argparse
import json
import os
import uuid

import app as webapp  # loads .env
from checker.audit import audit


def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("name")
    p.add_argument("type")
    p.add_argument("city", nargs="?", default="")
    p.add_argument("--engines", default="", help="comma list; default = all engines with a key")
    p.add_argument("--runs", type=int, default=None)
    a = p.parse_args()
    engines = [e.strip() for e in a.engines.split(",") if e.strip()] or None
    report = audit(a.url, a.name, a.type, a.city, engines=engines, runs=a.runs)
    rid = uuid.uuid4().hex[:12]
    report["id"] = rid
    path = os.path.join(webapp.DATA, "reports", rid + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    s = report["summary"]
    print(f"\n{report['business']['name']} — הופיע ב-{s['ai_mentions']} מתוך {s['ai_answers']} תשובות AI")
    for f in report["findings"]:
        print(f"\n[{f['section']}] {f['title']}")
        for item in f["items"][:8]:
            print("   -", item)
    print(f"\nנשמר: {path}\nלצפייה: http://127.0.0.1:5000/r/{rid}  (כשהאפליקציה רצה)")


if __name__ == "__main__":
    main()
