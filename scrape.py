#!/usr/bin/env python3
"""
VC job board tracker.

Scrapes the portfolio job boards of top VC firms (Consider- and Getro-powered),
filters for chief-of-staff / bizops / strategy-ops style roles in the configured
metros, dedupes against previously seen jobs, and writes:

  data/jobs.json       - all currently-matching jobs (with first_seen)
  data/new_roles.json  - jobs newly seen on THIS run (used for notifications)
  data/seen.json       - persistent dedupe state (job key -> first_seen ISO)
  data/status.json     - per-board health, so failures are visible
  docs/data.json       - copy of jobs.json for the GitHub Pages dashboard

Designed to run in GitHub Actions on a schedule. Run with --diagnose to dump
raw API shapes for debugging a board.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CONFIG = json.loads((ROOT / "config.json").read_text())
BOARDS = json.loads((ROOT / "boards.json").read_text())

CATEGORY_RES = [(c["name"], re.compile(c["pattern"], re.I)) for c in CONFIG["categories"]]
EXCLUDE_RE = re.compile(CONFIG["exclude_title_pattern"], re.I)


# ----------------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_job_lists(obj, depth=0):
    """Recursively find lists of dicts that look like job postings.

    Robust to schema drift: we don't assume where the jobs array lives in the
    response, only that job objects have a title-ish key.
    """
    found = []
    if depth > 6:
        return found
    if isinstance(obj, list):
        dictish = [x for x in obj if isinstance(x, dict)]
        if dictish and any(("title" in x or "job_title" in x or "jobTitle" in x) for x in dictish):
            found.append(dictish)
        else:
            for x in obj[:50]:
                found.extend(find_job_lists(x, depth + 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(find_job_lists(v, depth + 1))
    return found


def first(*vals):
    for v in vals:
        if v:
            return v
    return None


def as_location_list(j):
    locs = first(j.get("locations"), j.get("searchable_locations"),
                 j.get("normalized_locations"), j.get("locationNames"))
    if isinstance(locs, str):
        locs = [locs]
    if not locs:
        single = first(j.get("location"), j.get("place"), j.get("city"))
        if isinstance(single, dict):
            single = first(single.get("name"), single.get("label"), single.get("city"))
        locs = [single] if single else []
    out = []
    for l in locs:
        if isinstance(l, dict):
            l = first(l.get("name"), l.get("label"), l.get("city"), l.get("location"))
        if l:
            out.append(str(l))
    return out


def parse_posted(j):
    """Best-effort extraction of a posted/created timestamp -> ISO string or None."""
    cand = first(j.get("created_at"), j.get("createdAt"), j.get("timeStamp"),
                 j.get("timestamp"), j.get("firstPublishedAt"), j.get("first_published_at"),
                 j.get("posted_at"), j.get("postedAt"), j.get("publication_date"),
                 j.get("published_at"), j.get("date_posted"))
    if cand is None:
        return None
    try:
        if isinstance(cand, (int, float)):
            # unix seconds or milliseconds
            ts = float(cand)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        s = str(cand).strip()
        if re.fullmatch(r"\d{10,13}", s):
            ts = float(s)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # ISO-ish
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def normalize_job(j, board, base_url):
    title = first(j.get("title"), j.get("job_title"), j.get("jobTitle"))
    if not title:
        return None
    company = j.get("company")
    org = j.get("organization")
    company_name = first(
        j.get("companyName"), j.get("company_name"),
        company.get("name") if isinstance(company, dict) else (company if isinstance(company, str) else None),
        org.get("name") if isinstance(org, dict) else None,
        j.get("employer"),
    ) or "Unknown company"
    url = first(j.get("url"), j.get("applyUrl"), j.get("apply_url"), j.get("jobUrl"),
                j.get("job_url"), j.get("absolute_url"), j.get("link"))
    if url and url.startswith("/"):
        url = base_url.rstrip("/") + url
    if not url:
        # fall back to a search link on the board so the row is still actionable
        url = base_url
    return {
        "title": str(title).strip(),
        "company": str(company_name).strip(),
        "locations": as_location_list(j),
        "url": url,
        "posted_at": parse_posted(j),
        "board": board["id"],
        "board_name": board["name"],
    }


# ----------------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------------

def categorize(title):
    if EXCLUDE_RE.search(title):
        return None
    for name, rx in CATEGORY_RES:
        if rx.search(title):
            return name
    return None


def classify_location(locations):
    """Return (metro_tag, keep) given a list of location strings."""
    blob = " | ".join(locations).lower()
    if not blob.strip():
        return ("Unknown", CONFIG.get("include_unknown_location", True))
    for metro, needles in CONFIG["metros"].items():
        for n in needles:
            if n in blob:
                return (metro, True)
    for pat in CONFIG["remote_patterns"]:
        if pat in blob:
            return ("Remote", CONFIG.get("include_remote", True))
    return ("Other", False)


def job_key(job):
    basis = job["url"]
    # URLs sometimes carry per-run tracking params; strip query string for stability
    basis = basis.split("?")[0]
    if basis.rstrip("/") in (b["url"].rstrip("/") for b in BOARDS):
        basis = f'{job["board"]}|{job["company"]}|{job["title"]}|{",".join(sorted(job["locations"]))}'
    return hashlib.sha1(basis.lower().encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------
# Consider adapter  (jobs.a16z.com, jobs.sequoiacap.com, ...)
# ----------------------------------------------------------------------------

def consider_board_slugs(html, board):
    slugs = []
    for pat in (r'"boardId"\s*:\s*"([^"]+)"',
                r'boards/co/([a-zA-Z0-9_-]+)',
                r'"board"\s*:\s*\{\s*"id"\s*:\s*"([^"]+)"'):
        slugs += re.findall(pat, html)
    slugs += board.get("slugs", [])
    seen, out = set(), []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def fetch_consider(board, session, diagnose=False):
    base = board["url"].rstrip("/")
    html = session.get(base, timeout=30).text
    endpoint = f"{base}/api-boards/search-jobs"
    working = None
    for slug in consider_board_slugs(html, board):
        for is_parent in (True, False):
            body = {"meta": {"size": 10},
                    "board": {"id": slug, "isParent": is_parent},
                    "query": {"promoteFeatured": True}}
            try:
                r = session.post(endpoint, json=body, timeout=30)
                if r.ok:
                    lists = find_job_lists(r.json())
                    if lists:
                        working = (slug, is_parent)
                        if diagnose:
                            print(f"  [diagnose] {board['id']}: slug={slug} isParent={is_parent} "
                                  f"sample keys={sorted(lists[0][0].keys())}")
                        break
            except Exception:
                continue
        if working:
            break
    if not working:
        raise RuntimeError(f"consider: no working board slug for {base} "
                           f"(tried {consider_board_slugs(html, board)})")
    slug, is_parent = working
    raw = {}
    for kw in CONFIG["api_search_keywords"]:
        body = {"meta": {"size": 100},
                "board": {"id": slug, "isParent": is_parent},
                "query": {"promoteFeatured": True, "searchQuery": kw}}
        try:
            r = session.post(endpoint, json=body, timeout=30)
            if not r.ok:
                continue
            for lst in find_job_lists(r.json()):
                for j in lst:
                    n = normalize_job(j, board, base)
                    if n:
                        raw[(n["company"], n["title"], n["url"])] = n
        except Exception:
            continue
        time.sleep(0.3)
    return list(raw.values())


# ----------------------------------------------------------------------------
# Getro adapter  (jobs.accel.com, careers.redpoint.com, *.getro.com, ...)
# ----------------------------------------------------------------------------

def getro_collection_ids(html, board):
    ids = []
    for pat in (r'"collection_?[iI]d"\s*:\s*"?(\d+)',
                r'"collection"\s*:\s*\{\s*"id"\s*:\s*(\d+)',
                r'api\.getro\.com/(?:api/)?v\d/collections/(\d+)',
                r'"network_id"\s*:\s*(\d+)',
                r'data-network-id="(\d+)"'):
        ids += re.findall(pat, html)
    if board.get("collection_id"):
        ids.append(str(board["collection_id"]))
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def fetch_getro(board, session, diagnose=False):
    base = board["url"].rstrip("/")
    html = session.get(base, timeout=30).text
    ids = getro_collection_ids(html, board)
    if not ids:
        raise RuntimeError(f"getro: no collection id found in {base}")
    working = None
    for cid in ids:
        for ep in (f"https://api.getro.com/v2/collections/{cid}/search/jobs",
                   f"https://api.getro.com/api/v2/collections/{cid}/search/jobs"):
            for body in ({"hitsPerPage": 20, "page": 0, "query": ""},
                         {"hitsPerPage": 20, "page": 0},
                         {"hitsPerPage": 20, "page": 0, "filters": {}}):
                try:
                    r = session.post(ep, json=body, timeout=30)
                    if r.ok:
                        lists = find_job_lists(r.json())
                        if lists:
                            working = (cid, ep)
                            if diagnose:
                                print(f"  [diagnose] {board['id']}: cid={cid} ep={ep} "
                                      f"sample keys={sorted(lists[0][0].keys())}")
                            break
                except Exception:
                    continue
            if working:
                break
        if working:
            break
    if not working:
        raise RuntimeError(f"getro: no working endpoint for {base} (ids tried: {ids})")
    cid, ep = working
    raw = {}
    for kw in CONFIG["api_search_keywords"]:
        for body in ({"hitsPerPage": 100, "page": 0, "query": kw},
                     {"hitsPerPage": 100, "page": 0, "filters": {"q": kw}}):
            try:
                r = session.post(ep, json=body, timeout=30)
                if not r.ok:
                    continue
                got = False
                for lst in find_job_lists(r.json()):
                    for j in lst:
                        n = normalize_job(j, board, base)
                        if n:
                            got = True
                            raw[(n["company"], n["title"], n["url"])] = n
                if got:
                    break  # this body variant works; don't double-hit
            except Exception:
                continue
        time.sleep(0.3)
    return list(raw.values())


ADAPTERS = {"consider": fetch_consider, "getro": fetch_getro}


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnose", action="store_true", help="print raw API shapes per board")
    ap.add_argument("--boards", help="comma-separated board ids to run (default: all)")
    args = ap.parse_args()

    only = set(args.boards.split(",")) if args.boards else None
    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)

    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    first_run = len(seen) == 0

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json, text/html"})

    all_jobs, status, new_roles = {}, {}, []
    run_ts = now_iso()

    for board in BOARDS:
        if only and board["id"] not in only:
            continue
        entry = {"name": board["name"], "platform": board["platform"], "ok": False,
                 "fetched": 0, "matched": 0, "error": None}
        try:
            jobs = ADAPTERS[board["platform"]](board, session, diagnose=args.diagnose)
            entry["fetched"] = len(jobs)
            for job in jobs:
                cat = categorize(job["title"])
                if not cat:
                    continue
                metro, keep = classify_location(job["locations"])
                if not keep:
                    continue
                job["category"] = cat
                job["metro"] = metro
                key = job_key(job)
                job["key"] = key
                if key not in seen:
                    seen[key] = run_ts
                    if not first_run:
                        new_roles.append(job)
                job["first_seen"] = seen[key]
                # keep the earliest-seen copy if duplicated across boards
                if key not in all_jobs:
                    all_jobs[key] = job
                entry["matched"] += 1
            entry["ok"] = True
        except Exception as e:
            entry["error"] = str(e)[:300]
            print(f"[WARN] {board['id']}: {e}", file=sys.stderr)
        status[board["id"]] = entry
        print(f"{board['id']:>16}: ok={entry['ok']} fetched={entry['fetched']} matched={entry['matched']}"
              + (f"  ERROR: {entry['error']}" if entry["error"] else ""))

    jobs_list = sorted(all_jobs.values(),
                       key=lambda j: (j["first_seen"], j.get("posted_at") or ""), reverse=True)

    ok_count = sum(1 for s in status.values() if s["ok"])
    payload = {
        "generated_at": run_ts,
        "first_run": first_run,
        "boards_ok": ok_count,
        "boards_total": len(status),
        "new_count": len(new_roles),
        "new_badge_hours": CONFIG.get("new_badge_hours", 48),
        "jobs": jobs_list,
        "status": status,
    }

    (DATA / "jobs.json").write_text(json.dumps(payload, indent=1))
    (DATA / "new_roles.json").write_text(json.dumps(new_roles, indent=1))
    (DATA / "status.json").write_text(json.dumps(
        {"generated_at": run_ts, "first_run": first_run, "boards": status}, indent=1))
    seen_path.write_text(json.dumps(seen, indent=1))
    (DOCS / "data.json").write_text(json.dumps(payload, indent=1))

    print(f"\nTotal matching jobs: {len(jobs_list)}   new this run: {len(new_roles)}"
          f"   boards ok: {ok_count}/{len(status)}")

    if ok_count == 0:
        print("[FATAL] every board failed — check status.json", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
