import re

import yaml

from .util import path


FILLER = re.compile(r"\b(?:intern(?:ship)?s?|co-?op|student|university|new grad|early career|program|summer|fall|winter|spring|"
                    r"20\d\d|\d{4}|the|and|for)\b|[^a-z0-9 ]", re.I)
GENERIC_ENG = re.compile(r"(?:(?:founding|general(?:ist)?|technical)\s+)?engineer(?:ing|s)?(?:\s+generalist)?", re.I)


class Profile:
    def __init__(self, cfg_path=None):
        cfg = yaml.safe_load(open(cfg_path or path("config", "profile.yaml")))
        c = lambda xs: [re.compile(x, re.I) for x in xs]
        self.intern = c(cfg["intern_patterns"])
        self.exclude = c(cfg["exclude_title_patterns"])
        self.tech = c(cfg["tech_patterns"])
        self.weak_tech = c(cfg.get("weak_tech_patterns") or [])
        self.non_cs = c(cfg.get("non_cs_patterns") or [])
        self.cs_categories = set(cfg.get("cs_categories") or [])
        self.non_us = c(cfg["non_us_patterns"])
        self.us = c(cfg["us_patterns"])
        self.canada = c(cfg["canada_patterns"])
        sc = cfg.get("score", {})
        self.score_title = [(re.compile(k, re.I), v) for k, v in (sc.get("title") or {}).items()]
        self.score_loc = [(re.compile(k, re.I), v) for k, v in (sc.get("location") or {}).items()]
        self.watch_bonus = sc.get("watchlist_bonus", 2)
        self.alert_max_age_hours = cfg.get("alert_max_age_hours", 48)
        self.alert_min_score = cfg.get("alert_min_score", 0)

    @staticmethod
    def _any(pats, s):
        return any(p.search(s) for p in pats)

    def is_intern(self, job):
        t = job["title"]
        et = (job.get("employment_type") or "").lower()
        return bool(job.get("known_intern")) or self._any(self.intern, t) or et in ("intern", "internship")

    def region(self, loc):
        """'us', 'canada', 'non_us', or 'unknown'."""
        l = loc or ""
        if not l.strip():
            return "unknown"
        us = self._any(self.us, l)
        ca = self._any(self.canada, l)
        non = self._any(self.non_us, l)
        if us:
            return "us"
        if ca and not non:
            return "canada"
        if non or ca:
            return "canada" if ca else "non_us"
        return "unknown"

    def is_tech(self, job, startup=False):
        """Clear CS words always count; ambiguous ones ("Engineering Intern") need a CS category or a YC startup."""
        t = job["title"]
        if self._any(self.tech, t):
            return True
        if not self._any(self.weak_tech, t) or self._any(self.non_cs, t):
            return False
        if job.get("category") in self.cs_categories:
            return True
        # at a YC startup a bare "Engineering Intern" is almost always software; "Harness Design Engineer" isn't
        return startup and GENERIC_ENG.fullmatch(FILLER.sub(" ", t).strip()) is not None

    def evaluate(self, job, watchlist=False, startup=False):
        """Return (match: bool, score: int, region: str, reason: str)."""
        t = job["title"]
        if not self.is_intern(job):
            return False, 0, "", "not intern"
        if self._any(self.exclude, t):
            return False, 0, "", "excluded title"
        if not self.is_tech(job, startup):
            return False, 0, "", "not tech"
        degrees = job.get("degrees")
        if degrees and all(d in ("Master's", "PhD", "MBA") for d in degrees):
            return False, 0, "", "grad-degree only"
        reg = self.region(job.get("location"))
        if reg == "non_us":
            return False, 0, reg, "non-US"
        s = 0
        for p, v in self.score_title:
            if p.search(t):
                s += v
        for p, v in self.score_loc:
            if p.search(job.get("location") or ""):
                s += v
        if watchlist:
            s += self.watch_bonus
        if reg == "canada":
            s -= 2
        return True, s, reg, "ok"


def term_of(title, given=None):
    if given:
        return given
    t = title.lower()
    for k in ("winter", "spring", "summer", "fall", "autumn"):
        m = re.search(rf"{k}\s*(?:/\s*\w+\s*)?(20\d\d)", t)
        if m:
            return f"{k.title()} {m.group(1)}"
    m = re.search(r"\b(20\d\d)\b", t)
    if m:
        return m.group(1)
    if "co-op" in t or "coop" in t:
        return "Co-op"
    return ""
