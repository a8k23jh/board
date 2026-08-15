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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
# Two-tier filtering: a SOFT match (domain-qualified ops role such as "Product
# Operations Manager") only disqualifies when the title has no STRONG positive
# phrase — so "Chief of Staff, Director of Legal Operations" survives.
SOFT_RE = re.compile(CONFIG["exclude_soft_pattern"], re.I) if CONFIG.get("exclude_soft_pattern") else None
STRONG_RE = re.compile(CONFIG["strong_positive_pattern"], re.I) if CONFIG.get("strong_positive_pattern") else None

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
        "_api": j,          # kept for enrichment, stripped before serialisation
    }


def categorize(title):
    if EXCLUDE_RE.search(title):
        return None
    if SOFT_RE and SOFT_RE.search(title) and not (STRONG_RE and STRONG_RE.search(title)):
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


def norm_text(s):
    """Aggressive normalization for identity matching across boards."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def job_key(job):
    """Content-based identity so the SAME role listed on several VC boards
    (a company backed by multiple firms) collapses to one tracker row."""
    basis = f'{norm_text(job["company"])}|{norm_text(job["title"])}|{job.get("metro", "")}'
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


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


CSRF_HEADER_NAMES = ["x-csrf-token", "csrf-token", "x-xsrf-token", "xsrf-token", "x-csrftoken"]
CSRF_TOKEN_ENDPOINTS = ["/api-boards/csrf", "/api-boards/csrf-token", "/api/csrf", "/csrf"]


def harvest_csrf(base, session, html, diag):
    """Collect candidate CSRF tokens: cookies, HTML meta tags, token endpoints.

    The boards answer 412 INVALID_CSRF, which is the classic double-submit
    pattern — a token arrives as a cookie and must be echoed in a header.
    """
    tokens = []
    cookies = session.cookies.get_dict()
    diag["cookies"] = {k: (v[:10] + "…") if len(v) > 10 else v for k, v in cookies.items()}
    for k, v in cookies.items():
        if "csrf" in k.lower() or "xsrf" in k.lower():
            tokens.append(("cookie:" + k, v))
    for pat in (r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"',
                r'"csrfToken"\s*:\s*"([^"]+)"',
                r'"csrf"\s*:\s*"([^"]+)"'):
        for v in re.findall(pat, html):
            tokens.append(("html", v))
    for path in CSRF_TOKEN_ENDPOINTS:
        try:
            r = session.get(base + path, timeout=15,
                            headers={"Accept": "application/json", "Referer": base + "/jobs"})
            if r.ok:
                try:
                    d = r.json()
                    for key in ("csrfToken", "token", "csrf", "value"):
                        if isinstance(d, dict) and isinstance(d.get(key), str):
                            tokens.append(("endpoint:" + path, d[key]))
                except Exception:
                    if 16 <= len(r.text.strip()) <= 200 and "<" not in r.text:
                        tokens.append(("endpoint:" + path, r.text.strip()))
                diag.setdefault("token_endpoints", []).append({"path": path, "status": r.status_code,
                                                               "head": r.text[:120]})
        except Exception:
            continue
    # a token endpoint may have set a new cookie
    for k, v in session.cookies.get_dict().items():
        if ("csrf" in k.lower() or "xsrf" in k.lower()) and not any(t[1] == v for t in tokens):
            tokens.append(("cookie2:" + k, v))
    diag["csrf_candidates"] = [{"src": s, "len": len(v)} for s, v in tokens]
    return tokens


def fetch_consider(board, session, diag):
    base = board["url"].rstrip("/")
    r0 = session.get(base + "/jobs", timeout=30)
    html = r0.text
    diag["homepage"] = {"status": r0.status_code, "bytes": len(html)}
    slugs = consider_board_slugs(html, board)
    diag["slugs_tried"] = slugs
    diag["attempts"] = []

    tokens = harvest_csrf(base, session, html, diag)
    base_headers = {"Origin": base, "Referer": base + "/jobs",
                    "Content-Type": "application/json", "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest"}
    # header variants to try, cheapest/likeliest first
    header_sets = [dict(base_headers)]
    for src, tok in tokens:
        for hn in CSRF_HEADER_NAMES:
            h = dict(base_headers)
            h[hn] = tok
            header_sets.append(h)

    endpoints = [f"{base}/api-boards/search-jobs", "https://consider.com/api-boards/search-jobs"]
    working = None
    for endpoint in endpoints:
        for hi, headers in enumerate(header_sets):
            for slug in slugs:
                body = {"meta": {"size": 10}, "board": {"id": slug, "isParent": True},
                        "query": {"promoteFeatured": True}}
                try:
                    r = session.post(endpoint, json=body, headers=headers, timeout=25)
                    used = [k for k in headers if k.lower() in CSRF_HEADER_NAMES]
                    diag["attempts"].append({"endpoint": endpoint, "slug": slug,
                                             "csrf_header": used[0] if used else None,
                                             "status": r.status_code, "body_head": r.text[:200]})
                    if r.ok:
                        try:
                            data = r.json()
                        except Exception:
                            continue
                        if isinstance(data, dict) and ("jobs" in data or find_job_lists(data)):
                            working = (endpoint, slug, headers)
                            break
                except Exception as e:
                    diag["attempts"].append({"endpoint": endpoint, "slug": slug,
                                             "error": str(e)[:150]})
                if len(diag["attempts"]) > 60:   # keep runtime bounded
                    break
            if working or len(diag["attempts"]) > 60:
                break
        if working:
            break
    if not working:
        raise RuntimeError(f"consider: CSRF/endpoint probe failed for {base} (see diag)")
    endpoint, slug, headers = working
    diag["working"] = {"endpoint": endpoint, "slug": slug,
                       "csrf_header": [k for k in headers if k.lower() in CSRF_HEADER_NAMES]}

    def post_raw(body):
        """POST and return the parsed JSON response, or None on failure."""
        try:
            r = session.post(endpoint, json=body, headers=headers, timeout=25)
            if not r.ok:
                return None
            return r.json()
        except Exception:
            return None

    def jobs_of(resp):
        return [j for lst in find_job_lists(resp or {}) for j in lst]

    def post_jobs(body):
        resp = post_raw(body)
        return None if resp is None else jobs_of(resp)

    def shape_of(resp):
        """Compact structural fingerprint of a response, for diagnostics."""
        if not isinstance(resp, dict):
            return {"type": type(resp).__name__}
        out = {"keys": sorted(resp.keys())[:15]}
        if isinstance(resp.get("meta"), dict):
            out["meta"] = {k: (v if isinstance(v, (int, float, str, bool)) else type(v).__name__)
                           for k, v in list(resp["meta"].items())[:12]}
        js = jobs_of(resp)
        if js:
            out["job_keys"] = sorted(js[0].keys())[:20]
            out["job_count"] = len(js)
        return out

    # "sequence" is Consider's actual cursor name: base64 of {"_id":…,"score":…}
    CURSOR_KEYS = ("sequence", "searchAfter", "search_after", "cursor", "next",
                   "nextCursor", "scrollId", "scroll_id", "after", "nextPageToken")

    def extract_cursor(resp):
        if not isinstance(resp, dict):
            return None, None
        meta = resp.get("meta")
        if isinstance(meta, dict):
            for k in CURSOR_KEYS:
                if meta.get(k) not in (None, "", []):
                    return ("meta", k), meta[k]
        for k in CURSOR_KEYS:
            if resp.get(k) not in (None, "", []):
                return ("top", k), resp[k]
        # ES search_after convention: sort values on the last hit
        js = jobs_of(resp)
        if js and isinstance(js[-1].get("sort"), list):
            return ("meta", "searchAfter"), js[-1]["sort"]
        return None, None

    def base_body(size=100, extra_meta=None):
        b = {"meta": {"size": size}, "board": {"id": slug, "isParent": True}}
        if extra_meta:
            b["meta"].update(extra_meta)
        return b

    # ---- empirically find a body shape whose results actually track the query.
    # v3 observed every keyword search returning the same 100 jobs (searchQuery
    # silently ignored), so measure instead of assume: a variant "works" if a
    # 'chief of staff' query returns mostly chief-of-staff titles.
    PROBE_KW = "chief of staff"

    def hit_rate(jobs):
        if not jobs:
            return -1.0
        return sum(1 for j in jobs if PROBE_KW in str(j.get("title", "")).lower()) / len(jobs)

    def v_search_query(kw):
        b = base_body(); b["query"] = {"promoteFeatured": True, "searchQuery": kw}; return b

    def v_search_query_bare(kw):
        b = base_body(); b["query"] = {"searchQuery": kw}; return b

    def v_search(kw):
        b = base_body(); b["query"] = {"search": kw}; return b

    def v_text(kw):
        b = base_body(); b["query"] = {"text": kw}; return b

    def v_q(kw):
        b = base_body(); b["query"] = {"q": kw}; return b

    def v_top_level(kw):
        b = base_body(); b["searchQuery"] = kw; b["query"] = {}; return b

    def v_keywords_list(kw):
        b = base_body(); b["query"] = {"keywords": [kw]}; return b

    variants = [("query.searchQuery+promote", v_search_query),
                ("query.searchQuery", v_search_query_bare),
                ("query.search", v_search),
                ("query.text", v_text),
                ("query.q", v_q),
                ("top.searchQuery", v_top_level),
                ("query.keywords", v_keywords_list)]

    best = None
    diag["search_probe"] = []
    for name, builder in variants:
        jobs = post_jobs(builder(PROBE_KW))
        rate = hit_rate(jobs) if jobs is not None else -1
        diag["search_probe"].append({"variant": name, "jobs": len(jobs or []),
                                     "kw_hit_rate": round(rate, 3)})
        if jobs and rate >= 0.3 and (best is None or rate > best[2]):
            best = (name, builder, rate)
        time.sleep(0.15)

    raw = {}

    def collect(jobs):
        for j in jobs or []:
            n = normalize_job(j, board, base)
            if n:
                raw[(n["company"], n["title"], n["url"])] = n
        # one full raw job object as ground truth for future field mapping
        if jobs and "sample_job" not in diag:
            diag["sample_job"] = {k: (v if isinstance(v, (str, int, float, bool, type(None)))
                                      else json.loads(json.dumps(v, default=str))[:5]
                                      if isinstance(v, list) else str(v)[:200])
                                  for k, v in list(jobs[0].items())[:60]}

    if best:
        name, builder, rate = best
        diag["search_mode"] = {"variant": name, "kw_hit_rate": round(rate, 3)}
        for kw in CONFIG["api_search_keywords"]:
            collect(post_jobs(builder(kw)))
            time.sleep(0.2)
        return list(raw.values())

    # ---- no search variant works: sweep the whole board via pagination.
    resp0 = post_raw({**base_body(), "query": {"promoteFeatured": True}})
    diag["response_shape"] = shape_of(resp0)
    page0 = jobs_of(resp0)
    titles0 = {str(j.get("title", "")) + str(j.get("companyName", "")) for j in page0}
    collect(page0)

    # 1) cursor-style pagination (Elasticsearch search_after and friends)
    where_key, cursor = extract_cursor(resp0)
    if where_key:
        diag["search_mode"] = {"variant": f"cursor:{where_key[0]}.{where_key[1]}"}
        resp = resp0
        for p in range(200):                         # sweep even 16k-job boards
            body = {**base_body(), "query": {"promoteFeatured": True}}
            if where_key[0] == "meta":
                body["meta"][where_key[1]] = cursor
            else:
                body[where_key[1]] = cursor
            resp = post_raw(body)
            jobs = jobs_of(resp)
            if not jobs:
                break
            before = len(raw)
            collect(jobs)
            if len(raw) == before:
                break
            where_key2, cursor = extract_cursor(resp)
            if not where_key2 or cursor is None:
                break
            where_key = where_key2
            time.sleep(0.15)
        if len(raw) > len(page0):
            return list(raw.values())

    # 2) offset-style pagination, in meta AND at the body top level
    page_mode = None
    offset_variants = [("meta.from", "meta", "from"), ("meta.page", "meta", "page"),
                       ("meta.offset", "meta", "offset"), ("top.offset", "top", "offset"),
                       ("top.from", "top", "from"), ("top.page", "top", "page"),
                       ("top.limit+offset", "top", "limit_offset")]
    for name, where, key in offset_variants:
        body = {**base_body(), "query": {"promoteFeatured": True}}
        if key == "limit_offset":
            body.pop("meta", None)
            body["limit"] = 100
            body["offset"] = 100
        elif where == "meta":
            body["meta"][key] = 2 if key == "page" else 100
        else:
            body[key] = 2 if key == "page" else 100
        nxt = post_jobs(body)
        if nxt:
            tn = {str(j.get("title", "")) + str(j.get("companyName", "")) for j in nxt}
            if tn and len(tn - titles0) > len(tn) * 0.5:
                page_mode = (name, where, key)
                break
        time.sleep(0.15)

    if page_mode:
        name, where, key = page_mode
        diag["search_mode"] = {"variant": f"paginate:{name}"}
        for p in range(1, 60):
            body = {**base_body(), "query": {"promoteFeatured": True}}
            if key == "limit_offset":
                body.pop("meta", None)
                body["limit"] = 100
                body["offset"] = p * 100
            elif where == "meta":
                body["meta"][key] = (p + 1) if key == "page" else p * 100
            else:
                body[key] = (p + 1) if key == "page" else p * 100
            jobs = post_jobs(body)
            if not jobs:
                break
            before = len(raw)
            collect(jobs)
            if len(raw) == before:
                break
            time.sleep(0.15)
        return list(raw.values())

    # 3) nothing paginates: capture bundle ground truth for an exact fix
    diag["search_mode"] = {"variant": "single-page"}
    try:
        srcs = re.findall(r'''(?:src|href)=["']([^"']+\.js[^"']*)["']''', html)
        srcs += re.findall(r'''["'](https?://[^"']+\.js)["']''', html)
        seen_s, srcs = set(), [s for s in srcs if not (s in seen_s or seen_s.add(s))]
        diag["script_srcs"] = srcs[:10]
        evidence, fetch_log = [], []
        for s in srcs[:6]:
            u = urljoin(base + "/", html_mod.unescape(s))
            try:
                rb = session.get(u, timeout=20)
                fetch_log.append({"url": u[-80:], "status": rb.status_code, "bytes": len(rb.text)})
                if not rb.ok:
                    continue
                t = rb.text
                for marker in ("search-jobs", "api-boards", "searchQuery", "promoteFeatured"):
                    for mm in list(re.finditer(re.escape(marker), t))[:2]:
                        evidence.append({"marker": marker,
                                         "ctx": t[max(0, mm.start() - 350): mm.start() + 350]})
                    if len(evidence) >= 8:
                        break
            except Exception as e:
                fetch_log.append({"url": u[-80:], "error": str(e)[:120]})
            if len(evidence) >= 8:
                break
        diag["bundle_fetch_log"] = fetch_log
        if evidence:
            diag["bundle_evidence"] = evidence
    except Exception as e:
        diag["bundle_error"] = str(e)[:200]
    return list(raw.values())


# ----------------------------------------------------------------------------
# Getro adapter — parse server-rendered /jobs?q=... HTML
# (confirmed: Getro boards SSR their listings and support a ?q= search param)
# ----------------------------------------------------------------------------

JOB_HREF_RE = re.compile(r'href="([^"]*?/companies/([^/"]+)/jobs/[^"]+)"')
EXT_HREF_RE = re.compile(r'href="([^"]*utm_medium=getro\.com[^"]*)"')
ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
COMPANY_HREF_RE = re.compile(r'href="[^"]*?/companies/([^/"?#]+)"[^>]*>(.*?)</a>', re.S)


HEXISH = re.compile(r"^(?=[0-9a-f]*\d)[0-9a-f]{4,}$", re.I)  # hex fragment WITH a digit


def prettify_slug(slug):
    """'pave-bank-2' -> 'Pave Bank';  'cyera-2-5f138ae4-3d15-429e' -> 'Cyera'.

    Getro appends disambiguating counters and UUID fragments to company slugs.
    Requiring a digit inside hex fragments keeps real words ('face', 'cafe').
    """
    parts = [p for p in re.split(r"[-_]+", slug) if p]
    while len(parts) > 1 and (HEXISH.match(parts[-1]) or re.fullmatch(r"\d+", parts[-1])):
        parts.pop()
    return " ".join(parts).strip().title() or slug.title()


JOBWORDS = {"staff", "lead", "leader", "chief", "manager", "director", "officer", "head",
            "operations", "strategy", "president", "principal", "associate", "analyst",
            "partner", "vp", "senior", "junior", "intern", "office", "development",
            "specialist", "coordinator", "executive", "engineer", "designer", "remote",
            "hybrid", "onsite", "fulltime", "posted", "apply", "featured"}
# NOTE: "new" is deliberately absent — it would truncate New York / New Orleans.


def clean_location(loc):
    """Trim job-title words and stray acronyms that precede a 'City, ST' match.

    The listing text is flattened before matching, so 'Chief of Staff to COO
    Needham, MA' can match with 'COO' glued on. Drop leading tokens that are
    all-caps acronyms or obvious title words; keep the city itself.
    """
    if "," not in loc:
        return None
    city, _, rest = loc.rpartition(",")
    words = city.split()
    while words and (words[0].lower() in JOBWORDS
                     or (words[0].isupper() and 1 < len(words[0]) <= 4)):
        words.pop(0)
    if not words:
        return None
    return f"{' '.join(words)},{rest}"


def company_from_url(url):
    """Last-resort company name from an external ATS URL's domain."""
    m = re.match(r"https?://([^/]+)", url or "")
    if not m:
        return None
    host = m.group(1).lower()
    host = re.sub(r"^(www|jobs|careers|boards|apply|job|hire|talent|recruiting)\.", "", host)
    for ats in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "myworkdayjobs.com",
                "smartrecruiters.com", "bamboohr.com", "jobvite.com", "icims.com", "rippling.com"):
        if host.endswith(ats):
            return None  # generic ATS host tells us nothing about the company
    base = host.split(".")[0]
    return base.replace("-", " ").title() if len(base) > 2 else None


# ----------------------------------------------------------------------------
# Enrichment: salary, company size, funding stage, seniority / years of experience
# ----------------------------------------------------------------------------

SALARY_RE = re.compile(
    r"[$£€]\s?(\d{1,3}(?:,\d{3})?(?:\.\d+)?)\s*([KkMm])?\s*(?:-|–|—|to)\s*"
    r"[$£€]?\s?(\d{1,3}(?:,\d{3})?(?:\.\d+)?)\s*([KkMm])?")
SINGLE_SALARY_RE = re.compile(r"[$£€]\s?(\d{2,3})[Kk]\b")
STAGE_RE = re.compile(
    r"\b(pre[\s-]?seed|seed|series\s+([a-j])\b|growth|late[\s-]stage|early[\s-]stage|public|ipo)\b", re.I)
SIZE_RANGE_RE = re.compile(r"\b(\d{1,5})\s*(?:-|–|to)\s*(\d{1,5})\s*(?:\+\s*)?employees\b", re.I)
SIZE_PLUS_RE = re.compile(r"\b(\d{1,5})\+?\s*employees\b", re.I)
YC_BATCH_RE = re.compile(r"\b([WSFX])(\d{2})\b")

SIZE_BUCKETS = [(10, "1-10"), (50, "11-50"), (200, "51-200"), (500, "201-500"),
                (1000, "501-1k"), (5000, "1k-5k"), (10**9, "5k+")]


def size_bucket(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    for hi, label in SIZE_BUCKETS:
        if n <= hi:
            return label
    return None


def parse_salary(text):
    """Return (min_k, max_k) in thousands USD, or (None, None).

    Handles '$120K - $150K', '$120,000 - $150,000', and a lone '$150K'.
    Non-USD symbols are parsed too (close enough for filtering); absurd
    values are rejected so '$5 - $10' hourly noise doesn't pollute.
    """
    if not text:
        return (None, None)

    def to_k(num, unit):
        v = float(num.replace(",", ""))
        if unit and unit.lower() == "m":
            return v * 1000
        if unit and unit.lower() == "k":
            return v
        return v / 1000.0 if v >= 1000 else v      # bare 150000 -> 150

    m = SALARY_RE.search(text)
    if m:
        lo, lu, hi, hu = m.group(1), m.group(2), m.group(3), m.group(4)
        lo_k, hi_k = to_k(lo, lu or hu), to_k(hi, hu or lu)
        if 20 <= lo_k <= 2000 and 20 <= hi_k <= 2000 and hi_k >= lo_k:
            return (round(lo_k), round(hi_k))
    m = SINGLE_SALARY_RE.search(text)
    if m:
        v = float(m.group(1))
        if 20 <= v <= 2000:
            return (round(v), round(v))
    return (None, None)


def parse_stage(text):
    if not text:
        return None
    m = STAGE_RE.search(text)
    if not m:
        return None
    tok = m.group(1).lower().replace("-", " ").strip()
    if tok.startswith("pre"):
        return "Pre-Seed"
    if tok == "seed":
        return "Seed"
    if tok.startswith("series") and m.group(2):
        letter = m.group(2).upper()
        return f"Series {letter}" if letter in "ABC" else "Series D+"
    if tok in ("public", "ipo"):
        return "Public"
    if "late" in tok or tok == "growth":
        return "Growth/Late"
    if "early" in tok:
        return "Early"
    return None


def parse_size(text):
    if not text:
        return None
    m = SIZE_RANGE_RE.search(text)
    if m:
        return size_bucket(int(m.group(2)))
    m = SIZE_PLUS_RE.search(text)
    if m:
        return size_bucket(int(m.group(1)))
    return None


# title -> (seniority label, typical years-of-experience band)
SENIORITY_RULES = [
    ("Intern",     r"\bintern(ship)?\b|\bco-?op\b|\bfellow\b", "0-1"),
    ("Executive",  r"\b(chief (?!of staff)\w+ officer|c[teofm]o\b|svp\b|senior vice president|"
                   r"evp\b|executive vice president|partner)\b", "15+"),
    ("VP",         r"\b(vp|vice president)\b", "10-15"),
    ("Director",   r"\b(director|head of|general manager|\bgm\b)\b", "8-12"),
    # (?<!of ) keeps "Chief of Staff" out of the "Staff Engineer" bucket
    ("Principal",  r"\b(principal|(?<!of )staff|senior manager|sr\.? manager|group manager)\b", "6-10"),
    ("Manager",    r"\b(manager|lead|senior associate|sr\.? associate|senior analyst|"
                   r"sr\.? analyst|senior specialist)\b", "4-7"),
    ("Associate",  r"\b(associate|analyst|specialist|coordinator|assistant|junior|jr\.?|"
                   r"entry|new grad|graduate)\b", "0-3"),
]
SENIORITY_RES = [(lbl, re.compile(pat, re.I), yrs) for lbl, pat, yrs in SENIORITY_RULES]
SENIORITY_ORDER = ["Intern", "Associate", "Manager", "Principal", "Director", "VP", "Executive"]


def infer_seniority(title, api_levels=None):
    """Seniority + typical YoE band. API-provided levels win when present."""
    if api_levels:
        blob = " ".join(str(x) for x in (api_levels if isinstance(api_levels, list) else [api_levels]))
        for lbl in ("Intern", "Executive", "VP", "Director", "Principal", "Manager", "Associate"):
            if re.search(lbl, blob, re.I):
                yrs = dict((l, y) for l, _, y in SENIORITY_RULES).get(lbl)
                return lbl, yrs
    t = title or ""
    # "Chief of Staff" is a role name, not a rank — decide it before the generic
    # rules so "Chief"/"Staff" don't drag it to Executive/Principal.
    if re.search(r"chief of staff", t, re.I):
        if re.search(r"\b(senior|sr\.?|principal)\b", t, re.I):
            return "Principal", "6-10"
        if re.search(r"\b(director|head of)\b", t, re.I):
            return "Director", "8-12"
        if re.search(r"\b(vp|vice president)\b", t, re.I):
            return "VP", "10-15"
        return "Manager", "4-8"
    for lbl, rx, yrs in SENIORITY_RES:
        if rx.search(t):
            return lbl, yrs
    return None, None


# ---- fit scoring against the profile in config.json -------------------------

FIT = CONFIG.get("fit_profile", {})


def score_fit(job):
    """0-100 fit score plus human-readable reasons. Purely additive and
    explainable — every point is attributable to one rule."""
    score, why = FIT.get("base_score", 35), []

    cat_w = FIT.get("category_weights", {})
    if job.get("category") in cat_w:
        d = cat_w[job["category"]]
        score += d
        if d:
            why.append(f"{job['category']} {d:+d}")

    sen = job.get("seniority")
    sen_w = FIT.get("seniority_weights", {})
    if sen and sen in sen_w:
        d = sen_w[sen]
        score += d
        why.append(f"{sen}-level {d:+d}")

    stage_w = FIT.get("stage_weights", {})
    if job.get("stage") in stage_w:
        d = stage_w[job["stage"]]
        score += d
        why.append(f"{job['stage']} {d:+d}")

    size_w = FIT.get("size_weights", {})
    if job.get("company_size") in size_w:
        d = size_w[job["company_size"]]
        score += d
        why.append(f"{job['company_size']} employees {d:+d}")

    metro_w = FIT.get("metro_weights", {})
    if job.get("metro") in metro_w:
        d = metro_w[job["metro"]]
        score += d
        if d:
            why.append(f"{job['metro']} {d:+d}")

    # Real "minimum years of experience" from the board, when published.
    mye = job.get("min_years_exp")
    if mye is not None:
        for band, d in sorted(FIT.get("years_exp_weights", {}).items(),
                              key=lambda kv: int(kv[0].split("-")[0])):
            lo_b, hi_b = (int(x) for x in band.split("-"))
            if lo_b <= mye <= hi_b:
                score += d
                if d:
                    why.append(f"wants {mye}+ yrs {d:+d}")
                break

    lo = job.get("salary_min")
    target = FIT.get("target_salary_min_k")
    if lo and target:
        if lo >= target:
            d = FIT.get("salary_bonus", 7)
            score += d
            why.append(f"pays ${lo}k+ {d:+d}")
        elif lo < target * 0.7:
            d = FIT.get("salary_penalty", -12)
            score += d
            why.append(f"below target (${lo}k) {d:+d}")

    kb = FIT.get("keyword_bonus", 4)
    for kw in FIT.get("bonus_title_keywords", []):
        if re.search(kw, job.get("title", ""), re.I):
            score += kb
            why.append(f"GTM/founder-office match {kb:+d}")
            break

    return max(0, min(100, score)), "; ".join(why)


def enrich(job, blob="", api=None):
    """Attach salary / stage / size / seniority to a job, from whatever the
    board provided: structured API fields first, listing text as fallback."""
    api = api or {}
    lo = hi = None
    for k in ("salaryMin", "salary_min", "minSalary", "compensationMin", "baseSalaryMin"):
        if isinstance(api.get(k), (int, float)):
            lo = api[k] / 1000 if api[k] > 2000 else api[k]
            break
    for k in ("salaryMax", "salary_max", "maxSalary", "compensationMax", "baseSalaryMax"):
        if isinstance(api.get(k), (int, float)):
            hi = api[k] / 1000 if api[k] > 2000 else api[k]
            break
    if lo is None and hi is None:
        for k in ("salary", "salaryRange", "compensation", "payRange", "salaryText"):
            v = api.get(k)
            if isinstance(v, str) and v.strip():
                lo, hi = parse_salary(v)
                if lo:
                    break
            if isinstance(v, dict):
                mn = v.get("min") or v.get("from") or v.get("minValue")
                mx = v.get("max") or v.get("to") or v.get("maxValue")
                if isinstance(mn, (int, float)):
                    lo = mn / 1000 if mn > 2000 else mn
                if isinstance(mx, (int, float)):
                    hi = mx / 1000 if mx > 2000 else mx
                if lo or hi:
                    break
    if lo is None and hi is None:
        lo, hi = parse_salary(blob)
    job["salary_min"] = round(lo) if lo else None
    job["salary_max"] = round(hi) if hi else None

    staff = api.get("companyStaffCount") or api.get("staffCount") or api.get("employeeCount")
    job["company_size"] = size_bucket(staff) if staff else parse_size(blob)

    stage_api = api.get("fundingLV") or api.get("fundingStage") or api.get("stage") or \
        api.get("lastFundingType")
    job["stage"] = (parse_stage(str(stage_api)) or (str(stage_api).title() if isinstance(stage_api, str)
                                                    and len(str(stage_api)) < 20 else None)) \
        if stage_api else parse_stage(blob)

    # Consider's `stages` list is coarse — it labels an 83-person company
    # "Growth" the same as a 5,000-person one. companyStaffCount is an exact
    # number, so when the two disagree, trust the headcount and treat the stage
    # as unknown rather than applying a late-stage penalty to a small company.
    if job["stage"] in ("Growth/Late", "Public") and job.get("company_size") in ("1-10", "11-50", "51-200"):
        job["stage_raw"] = job["stage"]
        job["stage"] = None

    sen, yrs = infer_seniority(job.get("title", ""),
                               api.get("jobSeniorities") or api.get("considerLevels"))
    job["seniority"] = sen
    # A real minimum-years figure from the board beats inferring from the title.
    mye = api.get("minYearsExp") or api.get("min_years_exp")
    job["min_years_exp"] = int(mye) if isinstance(mye, (int, float)) and 0 <= mye <= 40 else None
    job["years_exp"] = f"{job['min_years_exp']}+" if job["min_years_exp"] else yrs

    job["remote_ok"] = bool(re.search(r"\bremote\b", blob, re.I)) or job.get("metro") == "Remote"
    return job


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
        # Context window: this anchor -> next job anchor (job cards can be large,
        # so allow up to 8000 chars; the next-anchor bound prevents bleed).
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        window = html[m.start(): min(end, m.start() + 8000)]
        blob = strip_tags(window)
        company = None
        cm = COMPANY_HREF_RE.search(window)
        if cm:
            ctext = strip_tags(cm.group(2))
            company = ctext if (0 < len(ctext) < 80) else prettify_slug(cm.group(1))
        if not company and is_internal:
            company = prettify_slug(re.search(r"/companies/([^/\"]+)/jobs/", href).group(1))
        if not company:
            # external listing: look BACK for the owning company link, then fall
            # back to the destination domain
            back = html[max(0, m.start() - 1500): m.start()]
            bm = None
            for bm in COMPANY_HREF_RE.finditer(back):
                pass  # keep the last (nearest) match
            if bm:
                btext = strip_tags(bm.group(2))
                company = btext if (0 < len(btext) < 80) else prettify_slug(bm.group(1))
            company = company or company_from_url(url)
        company = company or "See listing"
        if HEXISH.search(company.replace(" ", "")) and len(company) > 24:
            company = prettify_slug(company.replace(" ", "-"))
        # location: "City, ST" patterns, spelled-out states, or Remote
        # up to 3 consecutive Capitalized words before the comma, so surrounding
        # sentence text isn't swallowed into the location string
        locs = re.findall(
            r"\b((?:[A-Z][A-Za-z.'\-]+ ){0,2}[A-Z][A-Za-z.'\-]+,\s*"
            r"(?:[A-Z]{2}\b|California|New York|Texas|Massachusetts|Washington|Colorado|Illinois))",
            blob[:3000])
        if re.search(r"\bremote\b", blob[:3000], re.I):
            locs.append("Remote")
        seen_l, dedup_l = set(), []
        for l in locs:
            l = l.strip()
            l = l if l == "Remote" else (clean_location(l) or "")
            if not l:
                continue
            k = l.lower()
            if k not in seen_l:
                seen_l.add(k)
                dedup_l.append(l)
        jobs.append({
            "title": title,
            "company": company,
            "locations": dedup_l[:3],
            "_context": blob[:3000].lower(),  # used for metro matching, then dropped
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
    # Structure evidence: raw HTML around the first job anchor. Lets the parser
    # be corrected against ground truth instead of guesswork if fields go missing.
    unknown_loc = sum(1 for j in raw.values() if not j["locations"])
    diag["parse_health"] = {"jobs": len(raw), "without_location": unknown_loc}
    if not raw or unknown_loc > len(raw) * 0.3:
        try:
            sample = session.get(f"{base}/jobs?q=chief+of+staff", timeout=30).text
            am = None
            for am in ANCHOR_RE.finditer(sample):
                if re.search(r"/companies/[^/\"]+/jobs/", am.group(1)):
                    break
            if am:
                diag["html_sample"] = sample[max(0, am.start() - 1200): am.start() + 4000]
            else:
                diag["html_sample"] = sample[:4000]
        except Exception:
            pass
    return list(raw.values())


# ----------------------------------------------------------------------------
# Y Combinator adapter — ycombinator.com/jobs is public (no login) and, unlike
# the VC boards, publishes salary on most listings.
# ----------------------------------------------------------------------------

YC_JOB_HREF = re.compile(r"/companies/([^/\"?#]+)/jobs/([^/\"?#]+)")
YC_ROLE_PATHS = ["operations", "finance", "product", "sales"]


def parse_yc_html(html, board):
    base = "https://www.ycombinator.com"
    matches = [m for m in ANCHOR_RE.finditer(html) if YC_JOB_HREF.search(m.group(1))]
    jobs, seen = [], set()
    for i, m in enumerate(matches):
        href, inner = m.group(1), m.group(2)
        title = strip_tags(inner)
        if not title or len(title) > 140:
            continue
        url = urljoin(base, html_mod.unescape(href.split("#")[0]))
        if url in seen:
            continue
        seen.add(url)
        end = matches[i + 1].start() if i + 1 < len(matches) else m.start() + 4000
        # YC puts company + batch BEFORE the title link, salary/location after
        window = html[max(0, m.start() - 2500): min(end, m.start() + 3000)]
        blob = strip_tags(window)
        slug = YC_JOB_HREF.search(href).group(1)
        company = prettify_slug(slug)
        cm = re.findall(r'href="/companies/([^/"?#]+)"[^>]*>(.*?)</a>', window, re.S)
        for cslug, ctext in cm:
            t = strip_tags(ctext)
            if t and len(t) < 60 and cslug == slug:
                company = t
                break
        batch = None
        bm = YC_BATCH_RE.search(blob)
        if bm:
            batch = bm.group(0)
        locs = re.findall(
            r"\b((?:[A-Z][A-Za-z.'\-]+ ){0,2}[A-Z][A-Za-z.'\-]+,\s*(?:[A-Z]{2}\b|California|New York|Texas))",
            blob)
        locs = [clean_location(l) or "" for l in locs]
        locs = [l for l in locs if l]
        if re.search(r"\bremote\b", blob, re.I):
            locs.append("Remote")
        jobs.append({
            "title": title, "company": company, "locations": locs[:3],
            "_context": blob[:2500], "url": url, "posted_at": None,
            "board": board["id"], "board_name": board["name"], "yc_batch": batch,
        })
    return jobs


def fetch_yc(board, session, diag):
    raw, ok_any = {}, False
    diag["attempts"] = []
    for role in YC_ROLE_PATHS:
        url = f"https://www.ycombinator.com/jobs/role/{role}"
        try:
            r = session.get(url, timeout=30)
            got = parse_yc_html(r.text, board) if r.ok else []
            diag["attempts"].append({"url": url, "status": r.status_code,
                                     "bytes": len(r.text), "jobs_parsed": len(got)})
            if r.ok:
                ok_any = True
            for j in got:
                raw[j["url"]] = j
        except Exception as e:
            diag["attempts"].append({"url": url, "error": str(e)[:200]})
        time.sleep(0.5)
    if not ok_any:
        raise RuntimeError("yc: all role pages failed")
    if not raw:
        try:
            diag["html_sample"] = session.get(
                "https://www.ycombinator.com/jobs/role/operations", timeout=30).text[:4000]
        except Exception:
            pass
    return list(raw.values())


ADAPTERS = {"consider": fetch_consider, "getro": fetch_getro, "yc": fetch_yc}


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
    # retry transient connection resets / 5xx (kleiner & battery dropped
    # connections on a prior run); allowed_methods=None retries POSTs too
    retry = Retry(total=3, connect=3, read=2, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=None)
    session.mount("https://", HTTPAdapter(max_retries=retry))

    all_jobs, status, new_roles = {}, {}, []
    run_ts = now_iso()

    for board in BOARDS:
        if only and board["id"] not in only:
            continue
        entry = {"name": board["name"], "platform": board["platform"], "ok": False,
                 "fetched": 0, "matched": 0, "error": None}
        diag = {"board": board["id"], "run": run_ts}
        try:
            # one retry after a pause: some boards drop the connection under load
            # (careers.ivp.com reset mid-run once) and succeed on a second pass
            try:
                jobs = ADAPTERS[board["platform"]](board, session, diag)
            except Exception as first_err:
                diag["first_attempt_error"] = str(first_err)[:200]
                print(f"[retry] {board['id']}: {str(first_err)[:90]}", file=sys.stderr)
                time.sleep(20)
                jobs = ADAPTERS[board["platform"]](board, session, diag)
                diag["succeeded_on_retry"] = True
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
                enrich(job, blob=context or " ".join(job.get("locations", [])),
                       api=job.pop("_api", None))
                job["fit"], job["fit_why"] = score_fit(job)
                key = job_key(job)
                job["key"] = key
                if key not in seen:
                    seen[key] = run_ts
                    if not first_run:
                        new_roles.append(job)
                job["first_seen"] = seen[key]
                if key not in all_jobs:
                    job["also_on"] = []
                    all_jobs[key] = job
                elif board["name"] not in all_jobs[key]["also_on"] \
                        and board["name"] != all_jobs[key]["board_name"]:
                    all_jobs[key]["also_on"].append(board["name"])
                entry["matched"] += 1
            entry["ok"] = True
        except Exception as e:
            entry["error"] = str(e)[:300]
            print(f"[WARN] {board['id']}: {e}", file=sys.stderr)
        status[board["id"]] = entry
        (DIAG / f"{board['id']}.json").write_text(json.dumps(diag, indent=1)[:60000])
        print(f"{board['id']:>16}: ok={entry['ok']} fetched={entry['fetched']} matched={entry['matched']}"
              + (f"  ERROR: {entry['error']}" if entry["error"] else ""))

    # strip internal-only fields before serialising
    for j in list(all_jobs.values()) + new_roles:
        j.pop("_context", None)
        j.pop("_api", None)

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
