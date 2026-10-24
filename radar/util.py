import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST_LIMITS = {"api.ashbyhq.com": 16, "boards-api.greenhouse.io": 16, "api.lever.co": 12, "api.smartrecruiters.com": 8}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def path(*p):
    return os.path.join(ROOT, *p)


def load_json(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(p, obj, indent=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=indent, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, p)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_time(v):
    """Accept ISO strings, epoch seconds/ms, or 'October 23, 2026' style dates. Returns aware datetime or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        try:
            return datetime.fromtimestamp(x, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return parse_time(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_relative_posted(s, ref=None):
    """Workday-style 'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if not s:
        return None
    ref = ref or now_utc()
    t = s.lower()
    if "today" in t or "just" in t or "hour" in t or "minute" in t:
        return ref
    if "yesterday" in t:
        return ref - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return ref - timedelta(days=int(m.group(1)))
    return None


def canonical_key(url):
    """Stable identity for a posting across sources (ATS feed vs Simplify)."""
    if not url:
        return None
    u = url.strip()
    sp = urlsplit(u)
    host = sp.netloc.lower().removeprefix("www.")
    p = sp.path
    q = sp.query
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", u)
    if m:
        return f"ashby:{m.group(1).lower()}:{m.group(2)}"
    m = re.search(r"lever\.co/([^/]+)/([0-9a-f-]{36})", u)
    if m:
        return f"lever:{m.group(1).lower()}:{m.group(2)}"
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)/jobs/(\d+)", u)
    if m:
        return f"gh:{m.group(2)}"
    m = re.search(r"[?&](?:gh_jid|token)=(\d+)", u)
    if m:
        return f"gh:{m.group(1)}"
    m = re.search(r"/job/.*_([A-Za-z0-9-]+?)(?:-\d+)?$", p)
    if "myworkday" in host and m:
        tenant = host.split(".")[0]
        return f"wd:{tenant}:{m.group(1).lower()}"
    m = re.search(r"amazon\.jobs/(?:[a-z-]+/)?jobs/(\d+)", u)
    if m:
        return f"amazon:{m.group(1)}"
    m = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", u)
    if m:
        return f"sr:{m.group(2)}"
    m = re.search(r"eightfold\.ai/careers/job/(\d+)|/careers/job/(\d+)", u)
    if m:
        return f"ef:{host}:{m.group(1) or m.group(2)}"
    m = re.search(r"jobs/results/(\d+)", u)
    if m and "google" in host:
        return f"google:{m.group(1)}"
    m = re.search(r"jobs\.apple\.com/.*/details/([0-9-]+)", u)
    if m:
        return f"apple:{m.group(1)}"
    return f"url:{host}{p.rstrip('/')}" + (f"?{q}" if q and not p.strip('/') else "")


class Http:
    """Small async HTTP helper with global + per-host concurrency limits and one retry."""

    def __init__(self, concurrency=48, per_host=6, timeout=20.0):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
            http2=False,
        )
        self.sem = asyncio.Semaphore(concurrency)
        self.per_host = per_host
        self.host_sems = {}
        self.stats = {"requests": 0, "errors": 0}

    def _hs(self, url):
        h = urlsplit(url).netloc
        if h not in self.host_sems:
            self.host_sems[h] = asyncio.Semaphore(HOST_LIMITS.get(h, self.per_host))
        return self.host_sems[h]

    async def request(self, method, url, **kw):
        async with self.sem, self._hs(url):
            for attempt in range(2):
                try:
                    self.stats["requests"] += 1
                    r = await self.client.request(method, url, **kw)
                    if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    return r
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt == 0:
                        await asyncio.sleep(1)
                        continue
                    self.stats["errors"] += 1
                    return None
        return None

    async def get_json(self, url, **kw):
        r = await self.request("GET", url, **kw)
        if r is None or r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def close(self):
        await self.client.aclose()


LEGAL = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|the|gmbh|plc|pbc)\b\.?", re.I)


def company_key(name):
    """Loose company identity for joining sources: 'Ramp, Inc.' and 'ramp' match."""
    return re.sub(r"[^a-z0-9]+", "", LEGAL.sub("", (name or "").lower()))


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def ts():
    return time.strftime("%H:%M:%S")
