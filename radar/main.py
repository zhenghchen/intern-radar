"""One polling run: fetch every board, filter for Zheng, diff against what we've seen, alert, write the board feed."""
import argparse
import asyncio
import os
import re
from datetime import timedelta

import yaml

from . import pay as P
from . import sources as S
from .filters import Profile, term_of
from .notify import notify
from .util import Http, path, load_json, save_json, now_utc, iso, parse_time, canonical_key, company_key, ts

AGGREGATORS = {"simplify", "vanshb03", "vc"}
STARTUP_ATS = re.compile(r"ashbyhq\.com|greenhouse\.io|lever\.co|workable\.com|ats\.rippling\.com|ycombinator\.com|wellfound\.com|gem\.com|dover\.com|breezy\.hr|recruitee\.com|teamtailor\.com|bamboohr\.com", re.I)
EXITED = re.compile(r"acqui|public|\bipo\b", re.I)
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


def company_type(rec, rules, max_team=2000):
    """bigtech (watchlist) / startup / other. Big companies use Ashby and Greenhouse too (Rivian, Robinhood),
    so a startup ATS alone isn't enough: the company must be VC- or YC-backed, or its board small."""
    if rec["watchlist"]:
        return "bigtech"
    if CAMPUS_BOARD.search(rec["company"]):  # a big company's separate intern board looks deceptively small
        return "other"
    if rec.get("yc") and (rec.get("team_size") or 0) <= rules.get("max_yc_team_size", 1000):
        return "startup"
    if rec.get("backers") and (rec.get("team_size") or 0) <= max_team:
        return "startup"
    roles = rec.get("open_roles")
    if roles and roles <= rules.get("max_open_roles", 80) and STARTUP_ATS.search(rec["url"]):
        return "startup"
    return "other"


def investor_scorer(sig):
    pats = [(re.compile(k, re.I), v) for k, v in (sig.get("investors") or {}).items()]

    def weigh(name):
        return max((v for p, v in pats if p.search(name)), default=0)

    def score(rec):
        """(signal, notable backers best-first). Best investor's weight, +1 for a second one weighing 2+."""
        names, keys = [], []
        for n in (rec.get("investors") or []) + (rec.get("vcs") or []) + (["Y Combinator"] if rec.get("yc") else []):
            k = company_key(n)
            if k and not any(k.startswith(o) or o.startswith(k) for o in keys):  # "Sequoia" == "Sequoia Capital"
                names.append(n)
                keys.append(k)
        ranked = sorted(((weigh(n), n) for n in names), key=lambda x: -x[0])
        ranked = [(w, n) for w, n in ranked if w > 0]
        sig_ = ranked[0][0] if ranked else 0
        if len(ranked) > 1 and ranked[1][0] >= 2:
            sig_ += 1
        if rec["watchlist"]:
            sig_ = max(sig_, sig.get("watchlist", 4))
        return sig_, [n for _, n in ranked][:4]
    return score


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
    vcb = cfg.get("vc_boards") or {}
    for domain, board, vc in vcb.get("consider") or []:
        add(f"vc:consider:{board}", lambda d=domain, b=board, v=vc: S.consider(http, d, b, v))
    for network, vc in vcb.get("getro") or []:
        add(f"vc:getro:{network}", lambda n=network, v=vc: S.getro(http, n, v))
    if vcb.get("a16z"):
        add("vc:a16z", lambda: S.a16z(http))
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
    # VC boards change slowly and Getro pages are small; read them every `every_minutes`, carry them forward otherwise
    vc_every = timedelta(minutes=(cfg.get("vc_boards") or {}).get("every_minutes", 30))
    vc_due = (t0 - (parse_time(state.get("vc_last")) or t0 - vc_every)) >= vc_every - timedelta(minutes=1)
    skipped = []

    async def go():
        http = Http(concurrency=args.concurrency, per_host=6)
        try:
            tasks = build_tasks(http, cfg, db)
            if not vc_due:
                skipped.extend(k for k in tasks if k.startswith("vc:"))
                for k in skipped:
                    del tasks[k]
            res = await fetch_all(tasks, args.limit_boards)
            return res, http.stats
        finally:
            await http.close()
    results, hstats = asyncio.run(go())

    if vc_due and any(k.startswith("vc:") and v is not None for k, v in results.items()):
        state["vc_last"] = iso(t0)
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
    small_max = (cfg.get("startup") or {}).get("max_open_roles", 60)
    sig_cfg = cfg.get("signal") or {}
    max_team = sig_cfg.get("max_team_size", 2000)
    for bkey, jobs in results.items():
        if not jobs:
            continue
        meta = board_meta(db, bkey)
        small_startup = bool(meta.get("yc")) or (bkey.split(":")[0] in ("ashby", "greenhouse", "lever", "lever_eu")
                                                 and len(jobs) <= small_max)
        for raw in jobs:
            raw_count += 1
            if not raw.get("url") or not raw.get("title"):
                continue
            w = is_watch(raw["company"])
            vc_small = bool(raw.get("vcs")) and (raw.get("team_size") or 0) <= max_team
            ok, score, region, _ = prof.evaluate(raw, watchlist=w, startup=small_startup or vc_small)
            if not ok:
                continue
            key = canonical_key(raw["url"])
            is_agg = raw["source"] in AGGREGATORS
            rec = merged.get(key)
            if rec is None:
                rec = merged[key] = {
                    "id": key, "company": raw["company"], "title": raw["title"], "location": raw["location"],
                    "region": region, "url": raw["url"], "posted_at": None, "agg_posted_at": None,
                    "fit": score, "watchlist": w, "term": term_of(raw["title"], raw.get("term")),
                    "source": raw["source"], "sources": [], "boards": [], "pay": raw.get("pay"),
                    "posted_text": raw.get("posted_text"), "vcs": [],
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
                rec["fit"] = max(score, rec["fit"])
            else:
                # direct source wins for identity fields and the posted date
                rec.update({"company": raw["company"], "title": raw["title"], "location": raw["location"] or rec["location"],
                            "url": raw["url"], "source": raw["source"], "region": region, "fit": max(score, rec["fit"])})
                if raw.get("pay"):
                    rec["pay"] = raw["pay"]
                if p:
                    rec["posted_at"] = iso(p)
            for v in raw.get("vcs") or []:
                if v not in rec["vcs"]:
                    rec["vcs"].append(v)
            for mk in ("team_size", "stage"):
                if raw.get(mk) and not rec.get(mk):
                    rec[mk] = raw[mk]
            for mk in ("yc", "team_size", "one_liner"):
                if meta.get(mk) and not rec.get(mk):
                    rec[mk] = meta[mk]

    # keep jobs from boards that failed this run, or weren't due (avoid flapping)
    for b in failed + skipped:
        for j in prev_by_board.get(b, []):
            if j["id"] not in merged:
                merged[j["id"]] = j

    # company facts (investors, stage, size, markets) from the VC company lists the discover job collects,
    # then company signal, startup label and the overall score (role fit + company signal)
    vc_cos = db.get("vc_companies") or {}
    signal_of = investor_scorer(sig_cfg)
    for rec in merged.values():
        info = vc_cos.get(company_key(rec["company"])) or {}
        rec["vcs"] = list(dict.fromkeys((rec.get("vcs") or []) + (info.get("vcs") or [])))
        rec["investors"] = info.get("investors") or rec.get("investors") or []
        for mk in ("stage", "team_size", "markets"):
            if info.get(mk) and not rec.get(mk):
                rec[mk] = info[mk]
        rec["open_roles"] = max((state["boards"].get(b, {}).get("open_roles", 0) for b in rec["boards"]
                                 if not b.startswith(("agg:", "vc:"))), default=0) or None
        rec["fit"] = rec.get("fit", rec.get("score", 0))  # records carried over from before fit existed
        rec["signal"], rec["backers"] = signal_of(rec)
        if EXITED.search(str(rec.get("stage") or "")) and not rec["watchlist"]:
            rec["signal"], rec["backers"] = 0, []  # acquired or public: old VC backing no longer means "strong startup"
        # stale postings sink: -1 past 30 days, -2 past 90 (often evergreen or already filled)
        posted = parse_time(rec.get("posted_at")) or parse_time(rec.get("agg_posted_at"))
        age = (t0 - posted).days if posted else 0
        rec["score"] = rec["fit"] + rec["signal"] - (2 if age > 90 else 1 if age > 30 else 0)
        rec["type"] = company_type(rec, cfg.get("startup") or {}, max_team)

    def best(rec):
        return rec["watchlist"] or (rec["signal"] >= prof.alert_min_signal and rec["fit"] >= prof.alert_min_fit)

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
            if not bootstrap and fresh and not backlog and best(rec):
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

    # prune stale state
    cut = t0 - timedelta(days=60)
    state["jobs"] = {k: v for k, v in state["jobs"].items() if (parse_time(v.get("last_seen")) or t0) > cut}

    # pay: read each role's own posting once, new roles first so their alerts carry it, then the best matches
    pay_cfg = cfg.get("pay") or {}
    pay_cache = state.setdefault("pay", {})
    pay_lookups = 0
    if pay_cfg.get("enabled", True):
        order = new_jobs + sorted((r for r in merged.values() if not r.get("is_new")), key=lambda r: -r["score"])
        domains = {h: d for h, d, _ in (cfg.get("custom") or {}).get("eightfold") or []}
        domains["apply.careers.microsoft.com"] = "microsoft.com"

        async def look():
            http = Http(concurrency=args.concurrency, per_host=4)
            try:
                return await P.enrich(http, order, pay_cache, t0, pay_cfg.get("per_run", 400),
                                      pay_cfg.get("recheck_days", 3), domains)
            finally:
                await http.close()
        pay_lookups = asyncio.run(look())
    for rec in merged.values():
        P.apply(rec, pay_cache)
    state["pay"] = {k: v for k, v in pay_cache.items() if k in state["jobs"]}

    def sort_key(r):
        return parse_time(r.get("posted_at")) or parse_time(r.get("agg_posted_at")) or parse_time(r.get("first_seen")) or t0
    out = sorted(merged.values(), key=sort_key, reverse=True)
    stats = {"boards_ok": len(ok_boards), "boards_failed": len(failed), "raw_postings": raw_count,
             "matches": len(out), "new": len(new_jobs), "requests": hstats["requests"], "pay_lookups": pay_lookups,
             "seconds": round((now_utc() - t0).total_seconds(), 1), "bootstrap": bootstrap}
    feed = {"generated_at": iso(t0), "stats": stats, "jobs": out}
    save_json(path("data", "jobs.json"), feed)
    save_json(path("data", "state.json"), state)
    log = load_json(path("data", "new_log.json"), [])
    for r in new_jobs:
        log.insert(0, {k: r.get(k) for k in ("id", "company", "title", "term", "location", "url", "posted_at", "first_seen",
                                             "score", "fit", "signal", "backers", "type")})
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
