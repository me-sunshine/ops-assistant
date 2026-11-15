"""Run the synthetic sample requests through the real workflow and measure the results.

Usage:  python eval/run_eval.py [--app http://127.0.0.1:8000] [--only R01,R04]

What it measures automatically:
  - workflow reliability      share of requests that came back with a usable AI result
  - processing time           seconds from submission to result, per request
  - stated-fact recall        share of answer-key facts found in the extracted items
  - missing-info recall       share of answer-key gaps found in the missing-information list
  - quote check               how many "stated" claims had a quote that was really in the request

What it cannot measure (needs a human reviewer): whether each item is correct and useful.
For that, review the requests in the app, then read /api/review-stats for the correction rate.
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))


def found(groups, haystack):
    hay = haystack.lower()
    return [any(k.lower() in hay for k in g) for g in groups]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="http://127.0.0.1:8000")
    ap.add_argument("--data", default=os.path.join(HERE, "..", "data", "sample_requests.json"))
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    samples = json.load(open(args.data))["requests"]
    if args.only:
        samples = [s for s in samples if s["id"] in args.only.split(",")]
    client = httpx.Client(base_url=args.app, timeout=30, trust_env=False)
    results = []

    for s in samples:
        d = s["requested_date"]
        form = {k: s[k] for k in ("project_name", "department", "description", "priority", "stakeholders", "business_objective")}
        form["requested_date"] = (date.today() + timedelta(days=int(d))).isoformat() if d.startswith("+") else d
        form["submitted_by"] = "Evaluation script"
        t0 = time.time()
        resp = client.post("/requests", data=form)
        if resp.status_code != 303:
            results.append({"id": s["id"], "ok": False, "error": f"intake returned {resp.status_code}"})
            continue
        rid = int(resp.headers["location"].rsplit("/", 1)[1])
        while True:
            time.sleep(10)
            r = client.get(f"/api/requests/{rid}").json()
            if r["status"] in ("needs_review", "reviewed", "failed") or time.time() - t0 > args.timeout:
                break
        ok = r["status"] in ("needs_review", "reviewed")
        ai = [i for i in r["items"] if i["provenance"] != "system_check"]
        extracted = " | ".join(f"{i['ai_text']} {i['ai_owner'] or ''}" for i in ai
                               if i["kind"] in ("objective", "requirement", "task", "dependency"))
        gaps_ai = " | ".join(i["ai_text"] for i in ai if i["kind"] == "missing_info")
        gaps_all = " | ".join(i["ai_text"] for i in r["items"] if i["kind"] == "missing_info")
        exp = s["expected"]
        res = {
            "id": s["id"], "request_id": rid, "ok": ok, "status": r["status"], "error": r.get("error"),
            "seconds": r.get("processing_seconds"),
            "items_proposed": len(ai),
            "stated_verified": sum(i["provenance"] == "stated" for i in ai),
            "stated_downgraded": r.get("downgraded_claims"),
            "facts_expected": len(exp["stated_facts"]),
            "facts_found": sum(found(exp["stated_facts"], extracted)) if ok else 0,
            "facts_missed": [g[0] for g, f in zip(exp["stated_facts"], found(exp["stated_facts"], extracted)) if not f] if ok else None,
            "gaps_expected": len(exp["gaps"]),
            "gaps_found_ai": sum(found(exp["gaps"], gaps_ai)) if ok else 0,
            "gaps_found_with_system_checks": sum(found(exp["gaps"], gaps_all)) if ok else 0,
            "gaps_missed": [g[0] for g, f in zip(exp["gaps"], found(exp["gaps"], gaps_all)) if not f] if ok else None,
        }
        results.append(res)
        print(json.dumps(res), flush=True)

    done = [r for r in results if r["ok"]]
    secs = [r["seconds"] for r in done if r["seconds"]]
    tot = lambda k: sum(r[k] or 0 for r in done)
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": (client.get(f"/api/requests/{done[0]['request_id']}").json()["model"] if done else None),
        "requests_submitted": len(results), "requests_completed": len(done),
        "reliability": round(len(done) / len(results), 3) if results else None,
        "seconds_median": round(statistics.median(secs), 1) if secs else None,
        "seconds_min": min(secs) if secs else None, "seconds_max": max(secs) if secs else None,
        "stated_fact_recall": round(tot("facts_found") / tot("facts_expected"), 3) if done else None,
        "facts_found": tot("facts_found"), "facts_expected": tot("facts_expected"),
        "missing_info_recall_ai_only": round(tot("gaps_found_ai") / tot("gaps_expected"), 3) if done else None,
        "missing_info_recall_with_system_checks": round(tot("gaps_found_with_system_checks") / tot("gaps_expected"), 3) if done else None,
        "gaps_expected": tot("gaps_expected"),
        "items_proposed": tot("items_proposed"),
        "stated_claims_verified": tot("stated_verified"), "stated_claims_downgraded": tot("stated_downgraded"),
    }
    out = {"summary": summary, "per_request": results}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", f"eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(out, open(path, "w"), indent=2)
    print("\nSUMMARY\n" + json.dumps(summary, indent=2))
    print(f"\nSaved to {path}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
