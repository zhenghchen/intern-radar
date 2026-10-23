"""Daily: grow the list of company job boards to poll.

1. Every ATS link in the Simplify-format GitHub lists (internships + new grad) -> board slugs.
2. Every active YC company -> probe Ashby / Greenhouse / Lever with its slug; keep hits.
Results go to data/companies.json, which main.py polls every run."""
import argparse
import asyncio
import re
from collections import defaultdict
from datetime import timedelta

from .util import Http, path, load_json, save_json, now_utc, iso, parse_time, slugify, ts

LISTS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
]
YC_ALL = "https://yc-oss.github.io/api/companies/all.json"

PATS = {
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)"),
    "greenhouse": re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)/jobs"),
    "lever": re.compile(r"jobs\.lever\.co/([^/?#]+)"),
    "lever_eu": re.compile(r"jobs\.eu\.lever\.co/([^/?#]+)"),
    "smartrecruiters": re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)/"),
    "workday": re.compile(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)/"),
    "workday_site": re.compile(r"https://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)"),
}
SKIP_SLUGS = {"embed", "job_app", "jobs", "careers"}


def empty():
    return {"ashby": {}, "greenhouse": {}, "lever": {}, "lever_eu": {}, "smartrecruiters": {}, "workday": {},
            "yc_probe": {}, "updated": None}


def add(db, ats, key, name, extra=None):
    if key in SKIP_SLUGS:
        return False
    cur = db[ats].get(key)
    if cur is None:
        db[ats][key] = {"name": name, **(extra or {})}
        return True
    if extra:
        cur.update({k: v for k, v in extra.items() if v})
    return False


async def from_lists(http, db):
    n = 0
    recent_cut = now_utc() - timedelta(days=400)
    for url in LISTS:
        d = await http.get_json(url)
        if not isinstance(d, list):
            print(f"[{ts()}] list fetch failed: {url}")
            continue
        for j in d:
            u, name = j.get("url") or "", j.get("company_name") or ""
            posted = parse_time(j.get("date_posted"))
            if posted and posted < recent_cut:
                continue
            for ats, p in PATS.items():
                m = p.search(u)
                if not m:
                    continue
                if ats == "workday":
                    tenant, wd, site = m.groups()
                    n += add(db, "workday", f"{tenant}.{wd}.myworkdayjobs.com|{tenant}|{site}", name)
                elif ats == "workday_site":
                    wd, tenant, site = m.groups()
                    n += add(db, "workday", f"{wd}.myworkdaysite.com|{tenant}|{site}", name)
                else:
                    n += add(db, ats, m.group(1), name)
                break
    print(f"[{ts()}] lists: +{n} boards")


async def probe(http, ats, slug):
    if ats == "ashby":
        d = await http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        return d is not None and isinstance(d.get("jobs"), list)
    if ats == "greenhouse":
        d = await http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        return d is not None and isinstance(d.get("jobs"), list)
    if ats == "lever":
        d = await http.get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
        return isinstance(d, list)
    return False


async def from_yc(http, db, max_probe=3000, reprobe_days=14):
    d = await http.get_json(YC_ALL)
    if not isinstance(d, list):
        print(f"[{ts()}] YC list fetch failed")
        return
    known = {k.lower() for a in ("ashby", "greenhouse", "lever") for k in db[a]}
    cut = now_utc() - timedelta(days=reprobe_days)
    todo = []
    for c in d:
        if (c.get("status") or "").lower() != "active":
            continue
        name = c.get("name") or ""
        variants = {c.get("slug") or "", slugify(name).replace("-", ""), slugify(name)}
        variants = [v for v in variants if v and len(v) > 1]
        if any(v.lower() in known for v in variants):
            # already polled; just attach YC metadata
            for a in ("ashby", "greenhouse", "lever"):
                for v in variants:
                    if v in db[a]:
                        db[a][v].update(yc_meta(c))
            continue
        pkey = c.get("slug") or slugify(name)
        last = parse_time((db["yc_probe"].get(pkey) or {}).get("at"))
        if last and last > cut:
            continue
        todo.append((c, variants, pkey))
    todo = todo[:max_probe]
    print(f"[{ts()}] YC: probing {len(todo)} companies")
    hits = 0

    async def one(c, variants, pkey):
        nonlocal hits
        found = None
        for v in variants:
            for ats in ("ashby", "greenhouse", "lever"):
                if await probe(http, ats, v):
                    found = (ats, v)
                    break
            if found:
                break
        db["yc_probe"][pkey] = {"at": iso(now_utc()), "hit": f"{found[0]}:{found[1]}" if found else None}
        if found:
            hits += add(db, found[0], found[1], c.get("name"), yc_meta(c))

    await asyncio.gather(*(one(*t) for t in todo))
    print(f"[{ts()}] YC: +{hits} boards")


def yc_meta(c):
    return {"yc": c.get("batch"), "team_size": c.get("team_size"), "one_liner": c.get("one_liner"),
            "hq": c.get("all_locations") or c.get("location")}


async def run(skip_yc=False, max_probe=3000):
    db = load_json(path("data", "companies.json"), empty())
    for k, v in empty().items():
        db.setdefault(k, v)
    http = Http(concurrency=64, per_host=16, timeout=15)
    try:
        await from_lists(http, db)
        if not skip_yc:
            await from_yc(http, db, max_probe=max_probe)
    finally:
        await http.close()
    db["updated"] = iso(now_utc())
    save_json(path("data", "companies.json"), db, indent=1)
    print({k: len(v) for k, v in db.items() if isinstance(v, dict)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-yc", action="store_true")
    ap.add_argument("--max-probe", type=int, default=3000)
    a = ap.parse_args()
    asyncio.run(run(a.skip_yc, a.max_probe))
