"""Fetchers. Each returns a list of raw job dicts:
{company, title, location, url, posted_at (datetime|None), source, employment_type?}
or None if the fetch failed (so the caller can keep last-known jobs for that board)."""
import asyncio
import html
import re
from urllib.parse import quote

from .util import parse_time, parse_relative_posted, now_utc

INTERN_HINT = re.compile(r"intern|co-?op|student|summer 2027|university|campus|new grad|early career", re.I)


def J(company, title, location, url, posted_at, source, **extra):
    d = {"company": company, "title": (title or "").strip(), "location": (location or "").strip(),
         "url": url, "posted_at": posted_at, "source": source}
    d.update(extra)
    return d


# ---------------- Startup / mid-size ATS boards ----------------

async def ashby(http, slug, name):
    d = await http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(slug)}?includeCompensation=true")
    if d is None:
        return None
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
        if j.get("isRemote"):
            locs.append("Remote")
        comp = None
        c = j.get("compensation") or {}
        if isinstance(c, dict):
            comp = c.get("compensationTierSummary") or c.get("scrapeableCompensationSalarySummary")
        out.append(J(name, j.get("title"), "; ".join([l for l in locs if l]), j.get("jobUrl"),
                     parse_time(j.get("publishedAt")), "ashby", employment_type=j.get("employmentType"), pay=comp))
    return out


async def greenhouse(http, slug, name):
    d = await http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{quote(slug)}/jobs")
    if d is None:
        return None
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        posted = parse_time(j.get("first_published"))  # updated_at would make old jobs look new
        out.append(J(j.get("company_name") or name, j.get("title"), loc, j.get("absolute_url"), posted, "greenhouse"))
    return out


async def lever(http, slug, name, eu=False):
    host = "api.eu.lever.co" if eu else "api.lever.co"
    d = await http.get_json(f"https://{host}/v0/postings/{quote(slug)}?mode=json")
    if d is None or not isinstance(d, list):
        return None
    out = []
    for j in d:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location") or ""]
        if j.get("workplaceType") == "remote":
            locs = list(locs) + ["Remote"]
        out.append(J(name, j.get("text"), "; ".join([l for l in locs if l]), j.get("hostedUrl"),
                     parse_time(j.get("createdAt")), "lever", employment_type=cat.get("commitment")))
    return out


async def smartrecruiters(http, slug, name):
    out = []
    for q in ("intern", "co-op"):
        d = await http.get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(slug)}/postings?q={quote(q)}&limit=100")
        if d is None:
            return None
        for j in d.get("content", []):
            loc = j.get("location") or {}
            locs = ", ".join([x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x])
            if loc.get("remote"):
                locs += "; Remote"
            out.append(J(name, j.get("name"), locs, f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                         parse_time(j.get("releasedDate")), "smartrecruiters"))
    return out


async def workday(http, host, tenant, site, name, pages=1):
    """host is e.g. 'nvidia.wd5.myworkdayjobs.com' or 'wd1.myworkdaysite.com'."""
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    base = f"https://{host}/{site}" if "myworkdayjobs" in host else f"https://{host}/recruiting/{tenant}/{site}"
    out = []
    ref = now_utc()
    for q in ("intern", "co-op"):
        for page in range(pages):
            r = await http.request("POST", api, json={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": q},
                                   headers={"Content-Type": "application/json", "Accept": "application/json"})
            if r is None or r.status_code != 200:
                if page == 0 and q == "intern":
                    return None
                break
            try:
                d = r.json()
            except ValueError:
                return None
            posts = d.get("jobPostings") or []
            for j in posts:
                ep = j.get("externalPath") or ""
                out.append(J(name, j.get("title"), j.get("locationsText") or "", base + ep,
                             parse_relative_posted(j.get("postedOn"), ref), "workday", posted_text=j.get("postedOn")))
            if len(posts) < 20 or (page + 1) * 20 >= (d.get("total") or 0):
                break
    return out


# ---------------- Big tech custom endpoints ----------------

async def amazon(http):
    out, ok = [], False
    for q in ("intern", "co-op"):
        for off in (0, 100):
            d = await http.get_json(f"https://www.amazon.jobs/en/search.json?base_query={quote(q)}&country=USA&sort=recent&result_limit=100&offset={off}")
            if d is None:
                continue
            ok = True
            for j in d.get("jobs", []):
                company = "Amazon"
                cn = (j.get("company_name") or "")
                if "Web Services" in cn or "AWS" in (j.get("title") or ""):
                    company = "Amazon (AWS)"
                out.append(J(company, j.get("title"), j.get("normalized_location") or j.get("location"),
                             "https://www.amazon.jobs" + (j.get("job_path") or ""), parse_time(j.get("posted_date")), "amazon"))
    return out if ok else None


def _eightfold_job(host, name, source, j):
    url = j.get("canonicalPositionUrl") or j.get("positionUrl") or f"https://{host}/careers/job/{j.get('id')}"
    if url.startswith("/"):
        url = f"https://{host}" + url
    locs = j.get("locations") or j.get("standardizedLocations") or [j.get("location") or ""]
    return J(name, j.get("name") or j.get("posting_name"), "; ".join(map(str, locs)), url,
             parse_time(j.get("postedTs") or j.get("t_create") or j.get("creationTs")), source)


async def pcsx(http, host, domain, name, source, query="intern", max_pages=10):
    """Eightfold's newer search API (Microsoft, Qualcomm). Returns 10 positions per page."""
    out = []
    for page in range(max_pages):
        d = await http.get_json(f"https://{host}/api/pcsx/search?domain={domain}&query={quote(query)}"
                                f"&location=United%20States&start={page * 10}&sort_by=timestamp")
        if not isinstance(d, dict):
            return None if page == 0 else out
        dd = d.get("data") or d
        pos = dd.get("positions") or []
        out.extend(_eightfold_job(host, name, source, j) for j in pos)
        if len(pos) < 10 or (page + 1) * 10 >= (dd.get("count") or 0):
            break
    return out


async def eightfold(http, host, domain, name, query="intern", source="eightfold"):
    d = await http.get_json(f"https://{host}/api/apply/v2/jobs?domain={domain}&query={quote(query)}&num=100&sort_by=new")
    if d is None:
        return await pcsx(http, host, domain, name, source, query)  # tenants migrated off the v2 API
    return [_eightfold_job(host, name, source, j) for j in d.get("positions", [])]


async def microsoft(http):
    out = await pcsx(http, "apply.careers.microsoft.com", "microsoft.com", "Microsoft", "microsoft")
    if out:
        return out
    return await eightfold(http, "apply.careers.microsoft.com", "microsoft.com", "Microsoft", source="microsoft")


async def google(http):
    out, ok = [], False
    for page in (1, 2, 3):
        r = await http.request("GET", "https://www.google.com/about/careers/applications/jobs/results"
                               f"?q=intern&employment_type=INTERN&location=United%20States&sort_by=date&page={page}")
        if r is None or r.status_code != 200:
            continue
        ok = True
        text = r.text
        seen = set()
        for m in re.finditer(r'jobs/results/(\d+)-([a-z0-9-]+)', text):
            jid, slug = m.group(1), m.group(2)
            if jid in seen:
                continue
            seen.add(jid)
            title = slug.replace("-", " ").title()
            # try to find the real title near the link
            win = text[m.end(): m.end() + 1500]
            t = re.search(r"<h3[^>]*>(.*?)</h3>", win)
            if t:
                title = html.unescape(re.sub("<.*?>", "", t.group(1))).strip() or title
            out.append(J("Google", title, "United States", f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}",
                         None, "google"))
    return out if ok else None


async def apple(http):
    r = await http.request("GET", "https://jobs.apple.com/api/v1/CSRFToken")
    if r is None:
        return None
    tok = r.headers.get("x-apple-csrf-token")
    if not tok:
        return None
    out = []
    for page in (1, 2, 3):
        # a text query of "intern" also matches "internal"; the Students > Internships team filter is exact
        r2 = await http.request("POST", "https://jobs.apple.com/api/v1/search",
                                json={"query": "", "filters": {"locations": ["postLocation-USA"],
                                                               "teams": [{"team": "teamsAndSubTeams-STDNT", "subTeam": "subTeam-INTRN"}]},
                                      "page": page, "locale": "en-us", "sort": "newest",
                                      # without "format" the API silently returns zero results
                                      "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}},
                                headers={"x-apple-csrf-token": tok, "Content-Type": "application/json",
                                         "Origin": "https://jobs.apple.com", "Referer": "https://jobs.apple.com/en-us/search"},
                                cookies=r.cookies)
        if r2 is None or r2.status_code != 200:
            break
        try:
            res = (r2.json().get("res") or {}).get("searchResults") or []
        except ValueError:
            break
        for j in res:
            pid = j.get("positionId") or j.get("id")
            slug = j.get("transformedPostingTitle") or ""
            locs = "; ".join([l.get("name", "") for l in (j.get("locations") or []) if isinstance(l, dict)])
            out.append(J("Apple", j.get("postingTitle"), locs or "United States",
                         f"https://jobs.apple.com/en-us/details/{pid}/{slug}", parse_time(j.get("postingDate") or j.get("postDateInGMT")), "apple"))
        if len(res) < 20:
            break
    return out or None


async def uber(http):
    r = await http.request("POST", "https://www.uber.com/api/loadSearchJobsResults?localeCode=en",
                           json={"params": {"query": "intern", "location": [{"country": "USA"}]}, "page": 0, "limit": 50},
                           headers={"x-csrf-token": "x", "Content-Type": "application/json"})
    if r is None or r.status_code != 200:
        return None
    try:
        res = ((r.json().get("data") or {}).get("results")) or []
    except ValueError:
        return None
    out = []
    for j in res:
        loc = j.get("location") or {}
        locs = ", ".join([x for x in (loc.get("city"), loc.get("region"), loc.get("countryName")) if x])
        out.append(J("Uber", j.get("title"), locs, f"https://www.uber.com/global/en/careers/list/{j.get('id')}/",
                     parse_time(j.get("creationDate")), "uber"))
    return out


# ---------------- Aggregator backstops (Simplify-format GitHub lists) ----------------

SIMPLIFY_FEEDS = [
    ("simplify", "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"),
    ("vanshb03", "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json"),
]


async def simplify_feed(http, label, url):
    d = await http.get_json(url)
    if not isinstance(d, list):
        return None
    out = []
    for j in d:
        if not j.get("active", True) or j.get("is_visible") is False:
            continue
        tl = [t for t in (j.get("terms") or ([j["season"]] if j.get("season") else [])) if t and t.upper() != "N/A"]
        if tl and all(re.search(r"20(?:2[0-6])\b", t) for t in tl):
            continue  # only past/current-cycle terms (e.g. Summer 2026)
        terms = ", ".join(tl)
        out.append(J(j.get("company_name"), j.get("title"), "; ".join(j.get("locations") or []), j.get("url"),
                     parse_time(j.get("date_posted")), label, term=terms or None, known_intern=True,
                     degrees=j.get("degrees"), sponsorship=j.get("sponsorship")))
    return out
