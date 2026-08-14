#!/usr/bin/env python3
"""
VC job board tracker — v2.

Scrapes the portfolio job boards of top VC firms, filters for chief-of-staff /
bizops / strategy-ops style roles in the configured metros, dedupes against
previously seen jobs, and writes:

  data/jobs.json       - all currently-matching jobs (with first_seen)
  data/new_roles.json  - jobs newly seen on THIS run (used for notifications)
  data/seen.json       - persistent dedupe state (job key -> first_seen ISO)
  data/status.json     - per-board health, so failures are visible
  data/diag/*.json     - per-board diagnostics (request/response evidence)
  docs/data.json       - copy of jobs.json for the GitHub Pages dashboard

v2 changes:
  - Getro boards: parse the server-rendered HTML of /jobs?q=... (their pages
    SSR the listings; the JSON API is not discoverable from the homepage).
  - Consider boards: accept any JSON response containing a "jobs" key (an
    empty list is a VALID probe response), send Origin/Referer headers, try
    more endpoint variants.
  - Always exit 0 so the workflow's commit step runs and diagnostics are
    pushed even when boards fail. Health lives in data/status.json.
"""

import argparse
import hashlib
import html as html_mod
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DIAG = DATA / "diag"
DOCS = ROOT / "docs"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CONFIG = json.loads((ROOT / "config.json").read_text())
BOARDS = json.loads((ROOT / "boards.json").read_text())

CATEGORY_RES = [(c["name"], re.compile(c["pattern"], re.I)) for c in CONFIG["categories"]]
EXCLUDE_RE = re.compile(CONFIG["exclude_title_pattern"], re.I)

# Broader, shorter keyword list for HTML search (local regex does strict filtering)
GETRO_KEYWORDS = [
    "chief of staff", "business operations", "strategy", "operations",
    "founder", "corporate development", "general manager",
    "revenue operations", "special projects",
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# Generic helpers (unchanged from v1)
# ----------------------------------------------------------------------------

def find_job_lists(obj, depth=0):
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
    cand = first(j.get("created_at"), j.get("createdAt"), j.get("timeStamp"),
                 j.get("timestamp"), j.get("firstPublishedAt"), j.get("first_published_at"),
                 j.get("posted_at"), j.get("postedAt"), j.get("publication_date"),
                 j.get("published_at"), j.get("date_posted"))
    if cand is None:
        return None
    try:
        if isinstance(cand, (int, float)):
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


def categorize(title):
    if EXCLUDE_RE.search(title):
        return None
    for name, rx in CATEGORY_RES:
        if rx.search(title):
            return name
    return None


def classify_location(locations):
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
    basis = job["url"].split("?")[0]
    if basis.rstrip("/") in (b["url"].rstrip("/") for b in BOARDS):
        basis = f'{job["board"]}|{job["company"]}|{job["title"]}|{",".join(sorted(job["locations"]))}'
    return hashlib.sha1(basis.lower().encode()).hexdigest()[:16]


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return html_mod.unescape(re.sub(r"\s+", " ", s)).strip()


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


def fetch_consider(board, session, diag):
    base = board["url"].rstrip("/")
    r0 = session.get(base, timeout=30)
    html = r0.text
    diag["homepage"] = {"status": r0.status_code, "bytes": len(html)}
    headers = {"Origin": base, "Referer": base + "/jobs",
               "Content-Type": "application/json", "Accept": "application/json"}
    endpoints = [f"{base}/api-boards/search-jobs", f"{base}/api/boards/search-jobs"]
    slugs = consider_board_slugs(html, board)
    diag["slugs_tried"] = slugs
    diag["attempts"] = []
    working = None
    for endpoint in endpoints:
        for slug in slugs:
            for is_parent in (True, False):
                body = {"meta": {"size": 10},
                        "board": {"id": slug, "isParent": is_parent},
                        "query": {"promoteFeatured": True}}
                try:
                    r = session.post(endpoint, json=body, headers=headers, timeout=30)
                    rec = {"endpoint": endpoint, "slug": slug, "isParent": is_parent,
                           "status": r.status_code, "body_head": r.text[:300]}
                    diag["attempts"].append(rec)
                    if r.ok:
                        try:
                            data = r.json()
                        except Exception:
                            continue
                        # A "jobs" key ANYWHERE (even an empty list) = valid probe
                        if isinstance(data, dict) and ("jobs" in data or find_job_lists(data)):
                            working = (endpoint, slug, is_parent)
                            break
                except Exception as e:
                    diag["attempts"].append({"endpoint": endpoint, "slug": slug,
                                             "isParent": is_parent, "error": str(e)[:200]})
            if working:
                break
        if working:
            break
    if not working:
        raise RuntimeError(f"consider: no working endpoint/slug for {base} (see diag)")
    endpoint, slug, is_parent = working
    diag["working"] = {"endpoint": endpoint, "slug": slug, "isParent": is_parent}
    raw = {}
    for kw in CONFIG["api_search_keywords"]:
        body = {"meta": {"size": 100},
                "board": {"id": slug, "isParent": is_parent},
                "query": {"promoteFeatured": True, "searchQuery": kw}}
        try:
            r = session.post(endpoint, json=body, headers=headers, timeout=30)
            if not r.ok:
                continue
            for lst in find_job_lists(r.json()):
                for j in lst:
                    n = normalize_job(j, board, base)
                    if n:
                        raw[(n["company"], n["title"], n["url"])] = n
        except Exception:
            continue
        time.sleep(0.25)
    return list(raw.values())


# ----------------------------------------------------------------------------
# Getro adapter — parse server-rendered /jobs?q=... HTML
# (confirmed: Getro boards SSR their listings and support a ?q= search param)
# ----------------------------------------------------------------------------

JOB_HREF_RE = re.compile(r'href="([^"]*?/companies/([^/"]+)/jobs/[^"]+)"')
EXT_HREF_RE = re.compile(r'href="([^"]*utm_medium=getro\.com[^"]*)"')
ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
COMPANY_HREF_RE = re.compile(r'href="[^"]*?/companies/([^/"?#]+)"[^>]*>(.*?)</a>', re.S)


def prettify_slug(slug):
    return re.sub(r"[-_]+", " ", slug).strip().title()


def parse_getro_html(html, board, base):
    """Extract jobs from a server-rendered Getro listing page."""
    # first pass: find all job anchors so each context window can stop at the
    # next listing (otherwise job A picks up job B's company/location)
    matches = []
    for m in ANCHOR_RE.finditer(html):
        href = m.group(1)
        if re.search(r"/companies/[^/\"]+/jobs/", href) or "utm_medium=getro.com" in href:
            matches.append(m)
    jobs = []
    seen_urls = set()
    for i, m in enumerate(matches):
        href, inner = m.group(1), m.group(2)
        is_internal = bool(re.search(r"/companies/[^/\"]+/jobs/", href))
        title = strip_tags(inner)
        if not title or len(title) > 140 or title.lower() in ("apply", "apply now", "view job", "read more"):
            continue
        url = urljoin(base + "/", html_mod.unescape(href.split("#")[0]))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # context window after the anchor: usually holds company, location, date
        end = matches[i + 1].start() if i + 1 < len(matches) else m.start() + 2500
        window = html[m.start(): min(end, m.start() + 2500)]
        blob = strip_tags(window)
        company = None
        cm = COMPANY_HREF_RE.search(window)
        if cm:
            ctext = strip_tags(cm.group(2))
            company = ctext if (ctext and len(ctext) < 80) else prettify_slug(cm.group(1))
        if not company and is_internal:
            company = prettify_slug(re.search(r"/companies/([^/\"]+)/jobs/", href).group(1))
        company = company or "See listing"
        # location display: "City, ST" patterns or Remote in the context blob
        locs = re.findall(r"\b([A-Z][A-Za-z.\- ]+,\s*(?:[A-Z]{2}|California|New York|Texas))\b", blob[:600])
        if re.search(r"\bremote\b", blob[:600], re.I):
            locs.append("Remote")
        jobs.append({
            "title": title,
            "company": company,
            "locations": locs[:3],
            "_context": blob[:600].lower(),  # used for metro matching, then dropped
            "url": url,
            "posted_at": None,
            "board": board["id"],
            "board_name": board["name"],
        })
    return jobs


def fetch_getro(board, session, diag):
    base = board["url"].rstrip("/")
    diag["attempts"] = []
    raw = {}
    any_page_ok = False
    for kw in GETRO_KEYWORDS:
        for page in (1, 2):
            url = f"{base}/jobs?q={quote_plus(kw)}" + (f"&page={page}" if page > 1 else "")
            try:
                r = session.get(url, timeout=30)
                got = parse_getro_html(r.text, board, base) if r.ok else []
                diag["attempts"].append({"url": url, "status": r.status_code,
                                         "bytes": len(r.text), "jobs_parsed": len(got)})
                if r.ok:
                    any_page_ok = True
                for j in got:
                    raw[j["url"]] = j
                if len(got) < 10:
                    break  # thin page -> no need for page 2
            except Exception as e:
                diag["attempts"].append({"url": url, "error": str(e)[:200]})
                break
        time.sleep(0.4)
    if not any_page_ok:
        raise RuntimeError(f"getro: all page fetches failed for {base} (see diag)")
    if not raw:
        # pages loaded but zero jobs parsed -> capture evidence for debugging
        try:
            sample = session.get(f"{base}/jobs?q=chief+of+staff", timeout=30).text
            diag["html_sample"] = sample[:4000]
        except Exception:
            pass
    return list(raw.values())


ADAPTERS = {"consider": fetch_consider, "getro": fetch_getro}


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", help="comma-separated board ids to run (default: all)")
    args = ap.parse_args()

    only = set(args.boards.split(",")) if args.boards else None
    DATA.mkdir(exist_ok=True)
    DIAG.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)

    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    first_run = len(seen) == 0

    session = requests.Session()
    session.headers.update({"User-Agent": UA,
                            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9"})

    all_jobs, status, new_roles = {}, {}, []
    run_ts = now_iso()

    for board in BOARDS:
        if only and board["id"] not in only:
            continue
        entry = {"name": board["name"], "platform": board["platform"], "ok": False,
                 "fetched": 0, "matched": 0, "error": None}
        diag = {"board": board["id"], "run": run_ts}
        try:
            jobs = ADAPTERS[board["platform"]](board, session, diag)
            entry["fetched"] = len(jobs)
            for job in jobs:
                cat = categorize(job["title"])
                if not cat:
                    continue
                context = job.pop("_context", "")
                metro, keep = classify_location(job["locations"])
                if metro in ("Unknown", "Other") and context:
                    # fall back to scanning the listing's surrounding text
                    metro2, keep2 = classify_location([context])
                    if keep2:
                        metro, keep = metro2, keep2
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
                if key not in all_jobs:
                    all_jobs[key] = job
                entry["matched"] += 1
            entry["ok"] = True
        except Exception as e:
            entry["error"] = str(e)[:300]
            print(f"[WARN] {board['id']}: {e}", file=sys.stderr)
        status[board["id"]] = entry
        (DIAG / f"{board['id']}.json").write_text(json.dumps(diag, indent=1)[:60000])
        print(f"{board['id']:>16}: ok={entry['ok']} fetched={entry['fetched']} matched={entry['matched']}"
              + (f"  ERROR: {entry['error']}" if entry["error"] else ""))

    # strip any leftover context fields
    for j in all_jobs.values():
        j.pop("_context", None)
    for j in new_roles:
        j.pop("_context", None)

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
        print("[WARN] every board failed — diagnostics committed to data/diag/", file=sys.stderr)
    # Always exit 0 so the workflow commits diagnostics; health is in status.json


if __name__ == "__main__":
    main()
