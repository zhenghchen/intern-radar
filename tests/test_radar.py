import asyncio
import json
import os
import shutil
import types
from datetime import timedelta

import pytest

from radar import sources as S
from radar import main as M
from radar.filters import Profile
from radar.util import canonical_key, parse_relative_posted, now_utc, iso, parse_time


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers, self.cookies = data, status, headers or {}, {}
        self.text = data if isinstance(data, str) else json.dumps(data)

    def json(self):
        return self._d


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.stats = {"requests": 0, "errors": 0}

    def _find(self, url):
        for k, v in self.routes.items():
            if k in url:
                return v
        return None

    async def get_json(self, url, **kw):
        return self._find(url)

    async def request(self, method, url, **kw):
        d = self._find(url)
        return None if d is None else Resp(d)

    async def close(self):
        pass


def run(c):
    return asyncio.run(c)


# Fixtures mirror real API responses checked on Oct 23, 2026.
ASHBY = {"apiVersion": "1", "jobs": [
    {"id": "6037de96-d9c7-418f-ad23-56bb327225a6", "title": "Systems Engineer Intern", "employmentType": "Intern",
     "location": "San Francisco", "secondaryLocations": [], "publishedAt": "2026-07-02T00:32:06.045+00:00",
     "isListed": True, "isRemote": False, "jobUrl": "https://jobs.ashbyhq.com/dedalus-labs/6037de96-d9c7-418f-ad23-56bb327225a6"},
    {"id": "x", "title": "Systems Engineer", "employmentType": "FullTime", "location": "San Francisco",
     "publishedAt": "2026-06-29T17:10:57.337+00:00", "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/dedalus-labs/cc865f1c-3c3a-4e89-ab55-0021b0c34970"}]}
GH = {"jobs": [{"absolute_url": "https://job-boards.greenhouse.io/robinhood/jobs/8123225", "id": 8123225,
                "location": {"name": "Menlo Park, CA"}, "updated_at": "2026-10-02T18:48:15-04:00",
                "title": "Software Engineering Intern, Backend (Summer 2027)", "company_name": "Robinhood",
                "first_published": "2026-09-01T10:00:00-04:00"}]}
LEVER = [{"text": "Software Engineer Intern", "categories": {"commitment": "Intern", "location": "New York, NY",
          "allLocations": ["New York, NY"]}, "createdAt": 1786469891368, "id": "6ed76ce8-4156-4b60-b120-403538bd66cd",
          "hostedUrl": "https://jobs.lever.co/palantir/6ed76ce8-4156-4b60-b120-403538bd66cd", "workplaceType": "onsite"}]
AMZ = {"jobs": [{"title": "Software Development Engineer Intern, Amazon Leo - Summer 2027 (USA)", "company_name": "Amazon.com Services LLC",
                 "normalized_location": "Redmond, Washington, USA", "job_path": "/en/jobs/10587999/software-development-engineer-intern",
                 "posted_date": "October 23, 2026"}]}
EF = {"positions": [{"id": 790318904468, "name": "Software Engineer Intern - Summer 2027",
                     "locations": ["Los Gatos,California,United States of America"], "t_create": 1791849600,
                     "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790318904468"}]}
WD = {"total": 1, "jobPostings": [{"title": "Systems Software Engineering Intern", "externalPath": "/job/US-CA-Santa-Clara/Systems-Software-Engineering-Intern_JR2023492",
                                   "locationsText": "US, CA, Santa Clara", "postedOn": "Posted Yesterday"}]}


def test_ashby():
    out = run(S.ashby(FakeHttp({"ashbyhq.com/posting-api": ASHBY}), "dedalus-labs", "Dedalus Labs"))
    assert len(out) == 2 and out[0]["posted_at"].month == 7 and out[0]["employment_type"] == "Intern"


def test_greenhouse_uses_first_published():
    out = run(S.greenhouse(FakeHttp({"greenhouse.io": GH}), "robinhood", "Robinhood"))
    assert out[0]["posted_at"].month == 9


def test_lever_amazon_eightfold_workday():
    assert run(S.lever(FakeHttp({"lever.co": LEVER}), "palantir", "Palantir"))[0]["posted_at"] is not None
    a = run(S.amazon(FakeHttp({"amazon.jobs": AMZ})))
    assert a[0]["url"].startswith("https://www.amazon.jobs/en/jobs/10587999") and a[0]["posted_at"].day == 23
    e = run(S.eightfold(FakeHttp({"netflix.net": EF}), "explore.jobs.netflix.net", "netflix.com", "Netflix"))
    assert e[0]["posted_at"].year == 2026
    w = run(S.workday(FakeHttp({"myworkdayjobs.com": WD}), "nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite", "NVIDIA"))
    assert w[0]["url"] == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Systems-Software-Engineering-Intern_JR2023492"
    assert (now_utc() - w[0]["posted_at"]).days == 1


def test_canonical_keys_match_across_sources():
    a = canonical_key("https://jobs.ashbyhq.com/dedalus-labs/6037de96-d9c7-418f-ad23-56bb327225a6/application?embed=true&utm_source=Simplify")
    b = canonical_key("https://jobs.ashbyhq.com/dedalus-labs/6037de96-d9c7-418f-ad23-56bb327225a6")
    assert a == b
    assert canonical_key("https://www.mongodb.com/careers/job/?gh_jid=8209134&utm_source=Simplify") == canonical_key(
        "https://boards.greenhouse.io/mongodb/jobs/8209134")
    w1 = canonical_key("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Systems-Software-Intern_JR2005678?utm_source=Simplify")
    w2 = canonical_key("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Systems-Software-Intern_JR2005678")
    assert w1 == w2 == "wd:nvidia:jr2005678"
    assert canonical_key("https://amazon.jobs/en/jobs/10565667/sde-intern?utm_source=x") == canonical_key("https://www.amazon.jobs/en/jobs/10565667/sde-intern")


def test_filters():
    p = Profile()
    ok = lambda t, l="San Francisco, CA", **kw: p.evaluate({"title": t, "location": l, **kw})[0]
    assert ok("Software Engineer Intern")
    assert ok("Systems Software Engineer", employment_type="Intern")
    assert ok("Backend Engineer Co-op - Winter 2027", "Remote - US")
    assert not ok("Software Engineer Intern", "London, UK")
    assert not ok("PhD Research Scientist Intern")
    assert not ok("ASIC Design Intern")
    assert not ok("Marketing Intern")
    assert not ok("Software Engineer Intern - Summer 2026")
    assert not ok("Senior Software Engineer")
    assert ok("Machine Learning Operations Intern", "Madison, WI")
    assert ok("ML Engineer", "SF", known_intern=True)
    s_infra = p.evaluate({"title": "Infrastructure Software Engineer Intern", "location": "San Francisco"})[1]
    s_plain = p.evaluate({"title": "Software Engineer Intern", "location": "Chicago, IL"})[1]
    assert s_infra > s_plain


def test_regions():
    r = Profile().region
    for loc in ["Indianapolis, IN", "Vienna, VA", "Pomona, CA; Rosemead, CA", "Waterloo, IA; Cary, NC", "Remote", "Torrance"]:
        assert r(loc) == "us", loc
    for loc in ["Bengaluru, IN", "Berlin; Remote", "Kuala Lumpur, MY", "Remote in UK", "Munich, DE"]:
        assert r(loc) == "non_us", loc
    for loc in ["Toronto, ON, CA", "Ottawa, CA-ON, CA", "Remote; Canada"]:
        assert r(loc) == "canada", loc
    assert not Profile().evaluate({"title": "Software Engineer Intern", "location": "Toronto, ON"})[0]


def test_filters_cs_only():
    p = Profile()
    ok = lambda t, **kw: p.evaluate({"title": t, "location": "San Francisco, CA", **kw}, startup=kw.pop("startup", False))[0]
    # non-CS engineering never counts, even with a CS-sounding category or at a YC startup
    assert not ok("Roadway Engineering Intern - Summer 2027", known_intern=True)
    assert not ok("Structural Engineering Intern", category="Software")
    assert not ok("Quality Engineering Intern", startup=True)
    # bare "Engineering Intern" stays on the board but ranks lower, unless a CS category or small startup vouches for it
    fit = lambda t, **kw: p.evaluate({"title": t, "location": "San Francisco, CA", **kw}, startup=kw.pop("startup", False))[1]
    assert ok("Engineering Intern") and ok("Engineering Intern", category="Hardware")
    assert fit("Engineering Intern") < fit("Engineering Intern", category="Software") == fit("Engineering Intern", startup=True)
    assert ok("Engineering Intern", category="Software")
    assert ok("Engineering Intern", startup=True)
    assert ok("Summer 2027 Engineering Internship/Co-op", startup=True)
    assert ok("Systems Engineer Intern", startup=True)            # Dedalus Labs
    assert ok("Infrastructure Engineer Intern", startup=True)
    assert not ok("Harness Design Engineer Intern (Winter 2027)", startup=True)
    assert ok("Systems Engineer Intern", category="AI/ML/Data")
    # clear CS words count anywhere
    assert ok("Software Engineering Intern - Embedded Platforms")
    assert ok("Data Science Intern")
    assert ok("Quantitative Developer Intern")


def test_filters_bachelors_only():
    p = Profile()
    ok = lambda t, **kw: p.evaluate({"title": t, "location": "San Francisco, CA", **kw})[0]
    assert ok("Software Engineer Intern, Undergraduate - Summer 2027")
    assert ok("Forward Deployed Engineering Intern, BS/MS, Summer 2027")
    assert ok("Software Engineer Intern", degrees=["Bachelor's", "Master's"])
    assert not ok("Software Engineer Intern", degrees=["Master's", "PhD"])
    assert not ok("Compiler Engineering Graduate Intern")
    assert not ok("AI Data Science Intern, MS - Summer 2027")
    assert not ok("Research Intern - Machine Learning at MSR NYC")
    assert ok("Quantitative Strategies Research Intern")
    assert ok("Machine Learning Research Engineer Intern")


def test_google_embedded_rows():
    row = ["7566", "Forward Deployed Engineering Intern, BS/MS, Summer 2027", None, None,
           [None, "<h3>Minimum qualifications:</h3><ul><li>Currently pursuing a Bachelor's or Master's degree</li></ul>"],
           None, None, "Google", "en-US", [["Sunnyvale, CA, USA", []], ["Seattle, WA, USA", []]], None, None, [1791796170, 0]]
    page = "AF_initDataCallback({key: 'ds:1', hash: '2', data:" + json.dumps([[row], None, 1, 20]) + ", sideChannel: {}});"
    out = run(S.google(FakeHttp({"careers/applications/jobs/results": page})))
    j = out[0]
    assert j["title"].startswith("Forward Deployed") and j["location"] == "Sunnyvale, CA, USA; Seattle, WA, USA"
    assert j["degrees"] == ["Bachelor's", "Master's"] and j["posted_at"].year == 2026
    assert canonical_key(j["url"]) == "google:7566"


def test_slug_not_double_encoded():
    seen = []

    class H(FakeHttp):
        async def get_json(self, url, **kw):
            seen.append(url)
            return {"jobs": []}
    run(S.ashby(H({}), "Flock%20Safety", "Flock Safety"))
    assert "job-board/Flock%20Safety?" in seen[0]


def test_company_type():
    rules = {"max_open_roles": 80, "max_yc_team_size": 1000}
    rec = lambda **kw: {"watchlist": False, "company": "Acme", "url": "https://jobs.ashbyhq.com/x/1", **kw}
    assert M.company_type(rec(watchlist=True, open_roles=900), rules) == "bigtech"
    assert M.company_type(rec(open_roles=25), rules) == "startup"
    assert M.company_type(rec(open_roles=600), rules) == "other"      # e.g. Rivian on Ashby
    assert M.company_type(rec(open_roles=None), rules) == "other"     # only seen via an aggregator
    assert M.company_type(rec(yc="W24", team_size=40, open_roles=300), rules) == "startup"
    assert M.company_type(rec(yc="W09", team_size=6000, open_roles=300), rules) == "other"
    assert M.company_type(rec(open_roles=10, url="https://acme.wd1.myworkdayjobs.com/x"), rules) == "other"
    assert M.company_type(rec(open_roles=12, company="Klaviyo Campus"), rules) == "other"


def test_relative_posted():
    assert parse_relative_posted("Posted 30+ Days Ago") < now_utc() - timedelta(days=29)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    shutil.copytree(os.path.join(os.path.dirname(__file__), "..", "config"), root / "config")
    (root / "data").mkdir()
    (root / "docs").mkdir()
    import radar.util as U
    monkeypatch.setattr(U, "ROOT", str(root))
    return root


def fake_tasks(boards):
    def build(http, cfg, db):
        return {k: (lambda v=v: _ret(v)) for k, v in boards.items()}
    return build


async def _ret(v):
    return v


def test_end_to_end_alert_logic(sandbox, monkeypatch):
    sent = []

    async def fake_notify(jobs, board_url=None):
        sent.extend(jobs)
    monkeypatch.setattr(M, "notify", fake_notify)
    args = types.SimpleNamespace(no_notify=False, limit_boards=0, concurrency=4)
    t = now_utc()
    job = lambda i, title, posted, src="ashby", co="Acme": S.J(co, title, "San Francisco, CA",
                                                               f"https://jobs.ashbyhq.com/acme/{i:08d}-0000-0000-0000-000000000000", posted, src)
    json.dump({"vc_companies": {"acme": {"name": "Acme", "vcs": ["Sequoia"], "investors": ["Sequoia Capital"]}}},
              open(sandbox / "data" / "companies.json", "w"))
    # run 1: bootstrap -> no alerts, board filled
    monkeypatch.setattr(M, "build_tasks", fake_tasks({"ashby:acme": [job(1, "Software Engineer Intern", t - timedelta(days=30))],
                                                      "ashby:plain": [job(9, "Software Engineer Intern", t - timedelta(days=9), co="Plain")]}))
    st = M.run_once(args)
    assert st["matches"] == 2 and not sent
    # run 2: a fresh posting on a known board -> alert; an old posting -> silent; a brand-new board's backlog -> silent
    monkeypatch.setattr(M, "build_tasks", fake_tasks({
        "ashby:acme": [job(1, "Software Engineer Intern", t - timedelta(days=30)),
                       job(2, "Backend Engineer Intern", t - timedelta(hours=1)),
                       job(3, "Infrastructure Software Intern", t - timedelta(days=20))],
        "ashby:newco": [job(4, "Software Engineer Intern", None, co="NewCo")],
        "ashby:broken": None,
        # fresh role at a company with no notable backing: board only, no alert
        "ashby:plain": [job(9, "Software Engineer Intern", t - timedelta(days=9), co="Plain"),
                        job(10, "Backend Engineer Intern", t - timedelta(hours=1), co="Plain")],
    }))
    st = M.run_once(args)
    assert [j["title"] for j in sent] == ["Backend Engineer Intern"] and sent[0]["company"] == "Acme"
    assert sent[0]["backers"] == ["Sequoia Capital"] and sent[0]["signal"] == 3   # same fund under two names counts once
    feed = json.load(open(sandbox / "data" / "jobs.json"))
    assert feed["stats"]["matches"] == 6 and [j["title"] for j in feed["jobs"] if j["company"] == "Acme"][0] == "Backend Engineer Intern"
    # run 3: same data -> nothing new
    sent.clear()
    st = M.run_once(args)
    assert not sent and st["new"] == 0


def test_posted_date_keeps_earliest(sandbox, monkeypatch):
    # Workday says "Posted Today" on every run; the role must not jump back to the top each time
    args = types.SimpleNamespace(no_notify=True, limit_boards=0, concurrency=4)
    t = now_utc()
    job = lambda posted: S.J("Acme", "Software Engineer Intern", "San Francisco, CA",
                             "https://acme.wd1.myworkdayjobs.com/x/job/SF/SWE-Intern_R1", posted, "workday", posted_text="Posted Today")
    for posted in (t - timedelta(hours=3), t):
        monkeypatch.setattr(M, "build_tasks", fake_tasks({"workday:acme": [job(posted)]}))
        M.run_once(args)
    feed = json.load(open(sandbox / "data" / "jobs.json"))
    assert feed["jobs"][0]["posted_at"] == iso(t - timedelta(hours=3))


CONSIDER_JOBS = {"jobs": [{"companyName": "Cyera", "title": "Software Engineer Intern", "locations": ["New York, NY"],
                           "applyUrl": "https://jobs.ashbyhq.com/cyera/0b6f6a8e-1111-2222-3333-444455556666", "companyStaffCount": 1776,
                           "timeStamp": "2026-10-23T18:40:06Z"}], "meta": {"size": 50, "sequence": None}}
GETRO_JOBS = {"results": {"count": 1, "jobs": [{"title": "Backend Engineering Intern", "url": "https://boards.greenhouse.io/varda/jobs/8041944003",
                                                "created_at": 1792762370, "location_details": [{"name": "El Segundo, CA, USA"}],
                                                "organization": {"name": "Varda Space Industries", "stage": "series_b"}}]}}
A16Z_PAGE = ('<script>self.__next_f.push([1,' + json.dumps('x:["$","div",null,{"initialData":{"jobs":[{"title":"Software Engineering Intern",'
             '"apply_url":"https://job-boards.greenhouse.io/arcboatcompany/jobs/5442881008","location":"Torrance, CA","company_name":"Arc",'
             '"posted_at":"2026-10-02T21:56:54.000Z","funding_stage":{"label":"Growth"}}]}}]') + '])</script>')


def test_vc_sources():
    http = FakeHttp({"api-boards/search-jobs": CONSIDER_JOBS, "sequoiacap.com/jobs": 'x "csrfToken":"tok" x',
                     "api.getro.com": GETRO_JOBS, "jobs.a16z.com": A16Z_PAGE})
    c = run(S.consider(http, "jobs.sequoiacap.com", "sequoia-capital", "Sequoia Capital"))
    assert c[0]["company"] == "Cyera" and c[0]["vcs"] == ["Sequoia Capital"] and c[0]["posted_at"] is None
    g = run(S.getro(http, 222, "General Catalyst"))
    assert g[0]["location"] == "El Segundo, CA, USA" and g[0]["posted_at"].year == 2026 and g[0]["stage"] == "series_b"
    a = run(S.a16z(http))
    assert len(a) == 1 and a[0]["company"] == "Arc" and a[0]["vcs"] == ["Andreessen Horowitz"]
    assert run(S.consider(FakeHttp({}), "jobs.x.com", "x", "X")) is None


def test_company_signal_and_type():
    score = M.investor_scorer({"investors": {"sequoia|andreessen": 3, "y combinator|first round": 2, "sv angel": 1}, "watchlist": 4})
    rec = lambda **kw: {"watchlist": False, "company": "Acme", "url": "https://jobs.ashbyhq.com/acme/1", **kw}
    assert score(rec()) == (0, [])
    assert score(rec(yc="S25")) == (2, ["Y Combinator"])
    assert score(rec(vcs=["Sequoia Capital"], investors=["Sequoia Capital", "SV Angel"]))[0] == 3
    assert score(rec(vcs=["Andreessen Horowitz"], investors=["First Round Capital"]))[0] == 4
    assert score(rec(watchlist=True))[0] == 4   # (companies.yaml sets watchlist: 2 so startups rank first)
    rules = {"max_open_roles": 60, "max_yc_team_size": 1000}
    assert M.company_type(rec(backers=["Sequoia Capital"], team_size=300, open_roles=400), rules) == "startup"
    assert M.company_type(rec(backers=["Sequoia Capital"], team_size=2877), rules) == "other"   # Verkada-sized
    assert M.EXITED.search("Acquisition") and M.EXITED.search("Public") and not M.EXITED.search("Series B")
