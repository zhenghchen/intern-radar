"""Daily: grow the list of company job boards to poll.

1. Every ATS link in the Simplify-format GitHub lists (internships + new grad) -> board slugs.
2. Every active YC company -> probe Ashby / Greenhouse / Lever with its slug; keep hits.
3. VC portfolio boards -> each portfolio company's investors, stage and size (company signal),
   plus the ATS boards behind their internship postings.
4. Common Crawl -> every Ashby / Lever / Greenhouse board the web crawl has seen; probe, keep live ones.
Results go to data/companies.json, which main.py polls every run. data/coverage.json reports
whether each company in companies.yaml `must_cover` has a board being polled."""
import argparse
import asyncio
import json
import re
from datetime import timedelta
from urllib.parse import quote, unquote, urlsplit

import yaml

from . import sources as S
from .util import Http, path, load_json, save_json, now_utc, iso, parse_time, slugify, company_key, ts

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
    "workday": re.compile(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([A-Za-z0-9_-]+)/"),
    "workday_site": re.compile(r"https://(wd\d+)\.myworkdaysite\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?recruiting/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)"),
}
SKIP_SLUGS = {"embed", "job_app", "jobs", "careers", "api", "v1", "v0"}
CC_HOSTS = [("ashby", "jobs.ashbyhq.com"), ("lever", "jobs.lever.co"),
            ("greenhouse", "job-boards.greenhouse.io"), ("greenhouse", "boards.greenhouse.io")]


def empty():
    return {"ashby": {}, "greenhouse": {}, "lever": {}, "lever_eu": {}, "smartrecruiters": {}, "workday": {},
            "yc_probe": {}, "cc_probe": {}, "vc_companies": {}, "updated": None}


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
            n += add_url(db, u, name)
    print(f"[{ts()}] lists: +{n} boards")


def add_url(db, u, name, extra=None):
    """Record the ATS board behind a job URL. Returns True if it's a board we didn't know."""
    for ats, p in PATS.items():
        m = p.search(u or "")
        if not m:
            continue
        if ats == "workday":
            tenant, wd, site = m.groups()
            return add(db, "workday", f"{tenant}.{wd}.myworkdayjobs.com|{tenant}|{site}", name, extra)
        if ats == "workday_site":
            wd, tenant, site = m.groups()
            return add(db, "workday", f"{wd}.myworkdaysite.com|{tenant}|{site}", name, extra)
        return add(db, ats, m.group(1), name, extra)
    return False


async def probe(http, ats, slug):
    slug = quote(unquote(slug))
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


def _merge_vc(db, name, vc, investors=(), stage=None, team_size=None, markets=None):
    k = company_key(name)
    if not k:
        return
    rec = db["vc_companies"].setdefault(k, {"name": name, "vcs": [], "investors": []})
    if vc and vc not in rec["vcs"]:
        rec["vcs"].append(vc)
    rec["investors"] = list(dict.fromkeys(rec["investors"] + [i for i in investors if i]))
    for mk, v in (("stage", stage), ("team_size", team_size), ("markets", markets)):
        if v:
            rec[mk] = v


async def consider_companies(http, db, domain, board, vc):
    r = await http.request("GET", f"https://{domain}/companies")
    tok = re.search(r'"csrfToken":"([^"]+)"', r.text) if r is not None and r.status_code == 200 else None
    if not tok:
        return 0
    n, meta = 0, {"size": 100}
    for _ in range(30):
        r2 = await http.request("POST", f"https://{domain}/api-boards/search-companies",
                                json={"meta": meta, "board": {"id": board, "isParent": True}, "query": {}},
                                headers={"x-csrf-token": tok.group(1), "Accept": "application/json"}, cookies=r.cookies)
        try:
            d = r2.json() if r2 is not None and r2.status_code == 200 else {}
        except ValueError:
            d = {}
        cos = d.get("companies") or []
        for c in cos:
            stage = next((x for x in (c.get("stages") or []) if "employees" not in x), None)
            _merge_vc(db, c.get("name"), vc, c.get("investors") or [], stage, c.get("staffCount"), c.get("markets"))
            n += 1
        seq = (d.get("meta") or {}).get("sequence")
        if len(cos) < 100 or not seq:
            break
        meta = {"size": 100, "sequence": seq}
    return n


async def getro_companies(http, db, network, vc):
    n = 0
    for page in range(80):
        r = await http.request("POST", f"https://api.getro.com/api/v2/collections/{network}/search/companies",
                               json={"hitsPerPage": 20, "page": page, "query": ""}, headers={"Accept": "application/json"})
        try:
            res = (r.json().get("results") or {}) if r is not None and r.status_code == 200 else {}
        except ValueError:
            res = {}
        cos = res.get("companies") or []
        for c in cos:
            stage = (c.get("stage") or "").replace("_", " ").title() or None
            _merge_vc(db, c.get("name"), vc, (), None if stage in (None, "Series Unknown") else stage, None, c.get("industry_tags"))
            n += 1
        if len(cos) < 20 or (page + 1) * 20 >= (res.get("count") or 0):
            break
    return n


async def from_vc(http, db, cfg):
    """Portfolio companies of the funds in companies.yaml `vc_boards`, and the ATS boards behind their internships."""
    vcb = cfg.get("vc_boards") or {}
    jobs_tasks = [S.consider(http, d, b, v) for d, b, v in vcb.get("consider") or []]
    jobs_tasks += [S.getro(http, n, v) for n, v in vcb.get("getro") or []]
    if vcb.get("a16z"):
        jobs_tasks.append(S.a16z(http))
    co_counts = await asyncio.gather(*[consider_companies(http, db, d, b, v) for d, b, v in vcb.get("consider") or []],
                                     *[getro_companies(http, db, n, v) for n, v in vcb.get("getro") or []])
    boards = 0
    for jobs in await asyncio.gather(*jobs_tasks):
        for j in jobs or []:
            boards += add_url(db, j.get("url"), j.get("company"))
            _merge_vc(db, j.get("company"), (j.get("vcs") or [None])[0], (), j.get("stage"), j.get("team_size"))
    print(f"[{ts()}] VC: {sum(co_counts)} portfolio company rows, {len(db['vc_companies'])} companies known, +{boards} boards")


async def from_common_crawl(http, db, crawls=3, max_probe=2500, reprobe_days=30):
    """Every Ashby / Lever / Greenhouse board URL in the latest Common Crawl indexes. The index server is often
    overloaded (504s); whatever comes back is used and the rest waits for tomorrow."""
    info = await http.get_json("https://index.commoncrawl.org/collinfo.json")
    ids = [c["id"] for c in info[:crawls]] if isinstance(info, list) else []
    known = {a: {unquote(k).lower() for k in db[a]} for a in ("ashby", "lever", "greenhouse")}
    found = {a: set() for a in known}
    for cid in ids:
        for ats, host in CC_HOSTS:
            for attempt in range(3):
                r = await http.request("GET", f"https://index.commoncrawl.org/{cid}-index?url={host}/*&output=json&fl=url", timeout=180)
                if r is not None and r.status_code == 200:
                    break
                await asyncio.sleep(30 * (attempt + 1))
            if r is None or r.status_code != 200:
                print(f"[{ts()}] common crawl {cid} {host}: {getattr(r, 'status_code', 'no response')}")
                continue
            for line in r.text.splitlines():
                try:
                    slug = unquote(urlsplit(json.loads(line)["url"]).path.strip("/").split("/")[0])
                except (ValueError, KeyError, IndexError):
                    continue
                if slug and slug.lower() not in SKIP_SLUGS and "." not in slug[-4:]:
                    found[ats].add(slug)
    cut = now_utc() - timedelta(days=reprobe_days)
    todo = []
    for ats, slugs in found.items():
        for slug in slugs:
            pk = f"{ats}:{slug.lower()}"
            last = parse_time((db["cc_probe"].get(pk) or {}).get("at"))
            if slug.lower() not in known[ats] and not (last and last > cut):
                todo.append((ats, slug, pk))
    todo = todo[:max_probe]
    hits = 0

    async def one(ats, slug, pk):
        nonlocal hits
        ok = await probe(http, ats, slug)
        db["cc_probe"][pk] = {"at": iso(now_utc()), "hit": ok}
        if ok:
            hits += add(db, ats, slug, slug.replace("-", " ").replace("_", " ").title(), {"src": "common crawl"})
    await asyncio.gather(*(one(*t) for t in todo))
    print(f"[{ts()}] common crawl: {sum(map(len, found.values()))} boards seen, probed {len(todo)}, +{hits} boards")


def coverage(db, cfg):
    """Which `must_cover` companies have a board being polled (by name or slug)."""
    idx = {}
    for ats in ("ashby", "greenhouse", "lever", "lever_eu", "smartrecruiters", "workday"):
        for key, meta in db.get(ats, {}).items():
            for n in (meta.get("name"), key.split("|")[-1] if ats == "workday" else key):
                idx.setdefault(company_key(unquote(n or "")), f"{ats}:{key}")
    for ats, boards in (cfg.get("boards") or {}).items():
        for item in boards or []:
            slug, name = (item, boards[item]) if isinstance(boards, dict) else (item[2], item[3])
            for n in (slug, name):
                idx.setdefault(company_key(str(n)), f"{ats}:{slug}")
    report = {name: idx.get(company_key(name)) for name in cfg.get("must_cover") or []}
    save_json(path("data", "coverage.json"), report, indent=1)
    missing = [n for n, b in report.items() if not b]
    print(f"[{ts()}] coverage: {len(report) - len(missing)}/{len(report)} must-cover companies polled"
          + (f"; missing: {missing}" if missing else ""))


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


async def run(skip_yc=False, max_probe=3000, skip_cc=False):
    db = load_json(path("data", "companies.json"), empty())
    for k, v in empty().items():
        db.setdefault(k, v)
    cfg = yaml.safe_load(open(path("config", "companies.yaml")))
    http = Http(concurrency=64, per_host=16, timeout=15)
    try:
        await from_lists(http, db)
        await from_vc(http, db, cfg)
        if not skip_yc:
            await from_yc(http, db, max_probe=max_probe)
        if not skip_cc:
            await from_common_crawl(http, db)
    finally:
        await http.close()
    db["updated"] = iso(now_utc())
    save_json(path("data", "companies.json"), db, indent=1)
    coverage(db, cfg)
    print({k: len(v) for k, v in db.items() if isinstance(v, dict)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-yc", action="store_true")
    ap.add_argument("--max-probe", type=int, default=3000)
    ap.add_argument("--skip-cc", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.skip_yc, a.max_probe, a.skip_cc))
