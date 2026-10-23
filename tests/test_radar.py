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
    assert ok("Systems Engineer", employment_type="Intern")
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
    # run 1: bootstrap -> no alerts, board filled
    monkeypatch.setattr(M, "build_tasks", fake_tasks({"ashby:acme": [job(1, "Software Engineer Intern", t - timedelta(days=30))]}))
    st = M.run_once(args)
    assert st["matches"] == 1 and not sent
    # run 2: a fresh posting on a known board -> alert; an old posting -> silent; a brand-new board's backlog -> silent
    monkeypatch.setattr(M, "build_tasks", fake_tasks({
        "ashby:acme": [job(1, "Software Engineer Intern", t - timedelta(days=30)),
                       job(2, "Backend Engineer Intern", t - timedelta(hours=1)),
                       job(3, "Infra Intern", t - timedelta(days=20))],
        "ashby:newco": [job(4, "Software Engineer Intern", None, co="NewCo")],
        "ashby:broken": None,
    }))
    st = M.run_once(args)
    assert [j["title"] for j in sent] == ["Backend Engineer Intern"]
    feed = json.load(open(sandbox / "data" / "jobs.json"))
    assert feed["stats"]["matches"] == 4 and [j["title"] for j in feed["jobs"] if j["company"] == "Acme"][0] == "Backend Engineer Intern"
    # run 3: same data -> nothing new
    sent.clear()
    st = M.run_once(args)
    assert not sent and st["new"] == 0
