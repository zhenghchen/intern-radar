"""One polling run: fetch every board, filter for Zheng, diff against what we've seen, alert, write the board feed."""
import argparse
import asyncio
import os
import re
from datetime import timedelta

import yaml

from . import sources as S
from .filters import Profile, term_of
from .notify import notify
from .util import Http, path, load_json, save_json, now_utc, iso, parse_time, canonical_key, ts

AGGREGATORS = {"simplify", "vanshb03"}
STARTUP_ATS = re.compile(r"ashbyhq\.com|greenhouse\.io|lever\.co|workable\.com|ats\.rippling\.com|ycombinator\.com|wellfound\.com|gem\.com|dover\.com|breezy\.hr|recruitee\.com|teamtailor\.com|bamboohr\.com", re.I)
CAMPUS_BOARD = re.compile(r"campus|university|early career|students|college", re.I)
NOT_WATCH = re.compile(r"^(apple bank|snap finance|snap-on|western alliance|western union|hp hood|intelcom|meta(morphic|lenz))", re.I)


def watch_matcher(names):
    pats = [re.compile(rf"^{re.escape(n)}\b", re.I) for n in names]

    def is_watch(company):
        c = (company or "").strip()
        if NOT_WATCH.search(c):
            return False
        return any(p.search(c) for p in pats)
    return is_watch


def company_type(rec, rules):
    """bigtech (watchlist) / startup / other. Big companies use Ashby and Greenhouse too (Rivian, Robinhood),
    so a startup ATS alone isn't enough: the board must also be small, or the company a small YC team."""
    if rec["watchlist"]:
        return "bigtech"
    if CAMPUS_BOARD.search(rec["company"]):  # a big company's separate intern board looks deceptively small
        return "other"
    if rec.get("yc") and (rec.get("team_size") or 0) <= rules.get("max_yc_team_size", 1000):
        return "startup"
    roles = rec.get("open_roles")
    if roles and roles <= rules.get("max_open_roles", 80) and STARTUP_ATS.search(rec["url"]):
        return "startup"
    return "other"


def build_tasks(http, cfg, db):
    tasks = {}
    boards = cfg.get("boards") or {}
    seen = set()

    def add(key, coro_fn):
        if key in seen:
            return
        seen.add(key)
        tasks[key] = coro_fn

    for slug, name in (boards.get("greenhouse") or {}).items():
        add(f"greenhouse:{slug.lower()}", lambda s=slug, n=name: S.greenhouse(http, s, n))
    for slug, name in (boards.get("ashby") or {}).items():
        add(f"ashby:{str(slug).lower()}", lambda s=str(slug), n=name: S.ashby(http, s, n))
    for slug, name in (boards.get("lever") or {}).items():
        add(f"lever:{slug.lower()}", lambda s=slug, n=name: S.lever(http, s, n))
    for slug, name in (boards.get("smartrecruiters") or {}).items():
        add(f"smartrecruiters:{slug.lower()}", lambda s=slug, n=name: S.smartrecruiters(http, s, n))
    for row in boards.get("workday") or []:
        host, tenant, site, name = row[:4]
        pages = row[4] if len(row) > 4 else 1
        add(f"workday:{host}|{tenant}|{site}".lower(), lambda h=host, t=tenant, s=site, n=name, p=pages: S.workday(http, h, t, s, n, p))

    # auto-discovered boards
    for slug, meta in db.get("ashby", {}).items():
        add(f"ashby:{slug.lower()}", lambda s=slug, n=meta.get("name") or slug: S.ashby(http, s, n))
    for slug, meta in db.get("greenhouse", {}).items():
        add(f"greenhouse:{slug.lower()}", lambda s=slug, n=meta.get("name") or slug: S.greenhouse(http, s, n))
    for slug, meta in db.get("lever", {}).items():
        add(f"lever:{slug.lower()}", lambda s=slug, n=meta.get("name") or slug: S.lever(http, s, n))
    for slug, meta in db.get("lever_eu", {}).items():
        add(f"lever_eu:{slug.lower()}", lambda s=slug, n=meta.get("name") or slug: S.lever(http, s, n, eu=True))
    for slug, meta in db.get("smartrecruiters", {}).items():
        add(f"smartrecruiters:{slug.lower()}", lambda s=slug, n=meta.get("name") or slug: S.smartrecruiters(http, s, n))
    for key, meta in db.get("workday", {}).items():
        host, tenant, site = key.split("|")
        add(f"workday:{key}".lower(), lambda h=host, t=tenant, s=site, n=meta.get("name") or tenant: S.workday(http, h, t, s, n, 1))

    custom = cfg.get("custom") or {}
    if custom.get("amazon"):
        add("custom:amazon", lambda: S.amazon(http))
    if custom.get("google"):
        add("custom:google", lambda: S.google(http))
    if custom.get("apple"):
        add("custom:apple", lambda: S.apple(http))
    if custom.get("microsoft"):
        add("custom:microsoft", lambda: S.microsoft(http))
    if custom.get("uber"):
        add("custom:uber", lambda: S.uber(http))
    for host, domain, name in custom.get("eightfold") or []:
        add(f"eightfold:{host}", lambda h=host, d=domain, n=name: S.eightfold(http, h, d, n))
    if cfg.get("aggregators", True):
        for label, url in S.SIMPLIFY_FEEDS:
            add(f"agg:{label}", lambda l=label, u=url: S.simplify_feed(http, l, u))
    return tasks


def board_meta(db, board_key):
    ats, _, slug = board_key.partition(":")
    return (db.get(ats) or {}).get(slug) or (db.get(ats) or {}).get(slug.lower()) or {}


async def fetch_all(tasks, limit_boards=None):
    keys = list(tasks)[: limit_boards or None]
    results = {}

    async def one(k):
        try:
            results[k] = await tasks[k]()
        except Exception as e:  # one bad board must never sink the run
            print(f"[{ts()}] {k} crashed: {type(e).__name__}: {e}")
            results[k] = None
    await asyncio.gather(*(one(k) for k in keys))
    return results


def run_once(args):
    t0 = now_utc()
    prof = Profile()
    cfg = yaml.safe_load(open(path("config", "companies.yaml")))
    db = load_json(path("data", "companies.json"), {})
    state = load_json(path("data", "state.json"), {"jobs": {}, "boards": {}})
    prev = load_json(path("data", "jobs.json"), {"jobs": []})
    prev_by_board = {}
    for j in prev.get("jobs", []):
        for b in j.get("boards", []):
            prev_by_board.setdefault(b, []).append(j)
    is_watch = watch_matcher(cfg.get("watchlist") or [])
    bootstrap = not state["jobs"]

    async def go():
        http = Http(concurrency=args.concurrency, per_host=6)
        try:
            tasks = build_tasks(http, cfg, db)
            res = await fetch_all(tasks, args.limit_boards)
            return res, http.stats
        finally:
            await http.close()
    results, hstats = asyncio.run(go())

    ok_boards = [k for k, v in results.items() if v is not None]
    failed = [k for k, v in results.items() if v is None]
    new_boards = {k for k in results if not (state["boards"].get(k) or {}).get("last_ok")}
    for k in results:
        b = state["boards"].setdefault(k, {"fails": 0})
        if results[k] is None:
            b["fails"] = b.get("fails", 0) + 1
        else:
            b["fails"] = 0
            b["last_ok"] = iso(t0)
            b["open_roles"] = len(results[k])  # all roles, not just interns: a rough company-size signal

    merged = {}
    raw_count = 0
    for bkey, jobs in results.items():
        if not jobs:
            continue
        meta = board_meta(db, bkey)
        for raw in jobs:
            raw_count += 1
            if not raw.get("url") or not raw.get("title"):
                continue
            w = is_watch(raw["company"])
            ok, score, region, _ = prof.evaluate(raw, watchlist=w, startup=bool(meta.get("yc")))
            if not ok:
                continue
            key = canonical_key(raw["url"])
            is_agg = raw["source"] in AGGREGATORS
            rec = merged.get(key)
            if rec is None:
                rec = merged[key] = {
                    "id": key, "company": raw["company"], "title": raw["title"], "location": raw["location"],
                    "region": region, "url": raw["url"], "posted_at": None, "agg_posted_at": None,
                    "score": score, "watchlist": w, "term": term_of(raw["title"], raw.get("term")),
                    "source": raw["source"], "sources": [], "boards": [], "pay": raw.get("pay"),
                    "posted_text": raw.get("posted_text"),
                }
            if bkey not in rec["boards"]:
                rec["boards"].append(bkey)
            if raw["source"] not in rec["sources"]:
                rec["sources"].append(raw["source"])
            p = raw.get("posted_at")
            if is_agg:
                if p and (rec["agg_posted_at"] is None or p < parse_time(rec["agg_posted_at"])):
                    rec["agg_posted_at"] = iso(p)
                if rec.get("term") in ("", None) and raw.get("term"):
                    rec["term"] = raw["term"]
            else:
                # direct source wins for identity fields and the posted date
                rec.update({"company": raw["company"], "title": raw["title"], "location": raw["location"] or rec["location"],
                            "url": raw["url"], "source": raw["source"], "region": region, "score": max(score, rec["score"])})
                if raw.get("pay"):
                    rec["pay"] = raw["pay"]
                if p:
                    rec["posted_at"] = iso(p)
            for mk in ("yc", "team_size", "one_liner"):
                if meta.get(mk) and not rec.get(mk):
                    rec[mk] = meta[mk]

    # keep jobs from boards that failed this run (avoid flapping)
    for b in failed:
        for j in prev_by_board.get(b, []):
            if j["id"] not in merged:
                merged[j["id"]] = j

    # diff + alert decision
    new_jobs = []
    max_age = timedelta(hours=prof.alert_max_age_hours)
    for key, rec in merged.items():
        st = state["jobs"].get(key)
        if st is None:
            st = state["jobs"][key] = {"first_seen": iso(t0)}
            posted = parse_time(rec["posted_at"]) or parse_time(rec["agg_posted_at"])
            fresh = posted is None or (t0 - posted) <= max_age
            # a board polled for the first time shows its whole backlog; don't alert on that
            backlog = all(b in new_boards for b in rec["boards"])
            if not bootstrap and fresh and not backlog and rec["score"] >= prof.alert_min_score:
                new_jobs.append(rec)
                rec["is_new"] = True
                st["alerted"] = iso(t0)
        st["last_seen"] = iso(t0)
        # day-precision sources (Workday "Posted Today") would otherwise re-stamp a role as brand new every run
        if rec.get("posted_at"):
            if st.get("posted_at") and st["posted_at"] < rec["posted_at"]:
                rec["posted_at"] = st["posted_at"]
            st["posted_at"] = rec["posted_at"]
        rec["first_seen"] = st["first_seen"]
        rec["is_new"] = bool(rec.get("is_new")) and st.get("alerted") == iso(t0)
        if rec.get("agg_posted_at") and "agg_lead_hours" not in rec:
            lead = (parse_time(rec["agg_posted_at"]) - parse_time(rec["first_seen"])).total_seconds() / 3600
            if lead > 0.25:
                rec["agg_lead_hours"] = round(lead, 1)
        rec["open_roles"] = max((state["boards"].get(b, {}).get("open_roles", 0) for b in rec["boards"] if not b.startswith("agg:")),
                                default=0) or None
        rec["type"] = company_type(rec, cfg.get("startup") or {})

    # prune stale state
    cut = t0 - timedelta(days=60)
    state["jobs"] = {k: v for k, v in state["jobs"].items() if (parse_time(v.get("last_seen")) or t0) > cut}

    def sort_key(r):
        return parse_time(r.get("posted_at")) or parse_time(r.get("agg_posted_at")) or parse_time(r.get("first_seen")) or t0
    out = sorted(merged.values(), key=sort_key, reverse=True)
    stats = {"boards_ok": len(ok_boards), "boards_failed": len(failed), "raw_postings": raw_count,
             "matches": len(out), "new": len(new_jobs), "requests": hstats["requests"],
             "seconds": round((now_utc() - t0).total_seconds(), 1), "bootstrap": bootstrap}
    feed = {"generated_at": iso(t0), "stats": stats, "jobs": out}
    save_json(path("data", "jobs.json"), feed)
    save_json(path("data", "state.json"), state)
    log = load_json(path("data", "new_log.json"), [])
    for r in new_jobs:
        log.insert(0, {k: r.get(k) for k in ("id", "company", "title", "term", "location", "url", "posted_at", "first_seen", "score", "type")})
    save_json(path("data", "new_log.json"), log[:500], indent=1)
    print(f"[{ts()}] {stats}")
    bad = sorted(failed)[:25]
    if bad:
        print(f"[{ts()}] failed boards (first 25): {bad}")
    if new_jobs and not args.no_notify:
        asyncio.run(notify(new_jobs, os.environ.get("BOARD_URL")))
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--limit-boards", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=48)
    run_once(ap.parse_args())
