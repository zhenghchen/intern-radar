"""Pay lookup. Few job lists carry pay, so each matched role's own posting is read once (ATS API when the
URL names one, else the page itself) and the stated range is turned into dollars per hour.
Results are cached in state["pay"], so a role costs one request in its lifetime (plus rare rechecks)."""
import asyncio
import html
import json
import re
from datetime import timedelta
from urllib.parse import urlsplit

from .util import iso, parse_time

HOURS_PER = {"h": 1, "w": 40, "m": 2080 / 12, "y": 2080}

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s?([kK]\b)?"
# "$42.00 - $58.00", "$100,000 and $110,000", "USD $8,000 - 9,500", "$40/hour - $110/hour", "115,685.00 USD"
_MONEY = re.compile(
    r"(?:US\s?|USD\s?)?\$\s?" + _NUM + r"(?:\s?(?:/|per\s)\s?(?:hour|hr|month|mo|year|yr|week)\b)?"
    r"(?:\s*(?:-|–|—|to|and)\s*(?:US\s?|USD\s?)?\$?\s?" + _NUM + r")?"
    r"|" + r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s?USD", re.I)
_UNIT_AFTER = [("h", r"hour|hr\b|hourly"), ("m", r"month|mo\b|monthly"), ("w", r"week|weekly"),
               ("y", r"year|yr\b|annual|annually|salary|yearly")]
_UNIT_BEFORE = [("h", r"hourly|per hour|hour(?:ly)? rate|wage"), ("m", r"monthly|per month"),
                ("w", r"weekly|per week"), ("y", r"annual|yearly|per year|salary")]
_PAYWORDS = re.compile(r"pay|salary|compensation|wage|rate|stipend|range|earn|hourly|annual|base", re.I)
# amounts that aren't the role's pay
_NOT_PAY = re.compile(r"housing|relocation|sign(?:ing|-on)|bonus|referral|travel|reimburs|equipment|wellness|learning|"
                      r"raised|funding|revenue|valuation|series [a-e]|tuition|401", re.I)


def _amount(whole, cents, k):
    if not whole:
        return None
    v = float(whole.replace(",", "") + (f".{cents}" if cents else ""))
    return v * 1000 if k else v


def _unit(text, start, end, value):
    """(unit, stated): the pay period written next to an amount, else a guess from its size."""
    after = text[end:end + 30].lower()
    for u, p in _UNIT_AFTER:
        if re.match(rf"\W*(?:/|per|an?|each)?\s*(?:{p})", after):
            return u, True
    inside = text[start:end].lower()
    for u, p in _UNIT_AFTER:
        if re.search(rf"(?:/|per\s)\s?(?:{p})", inside):
            return u, True
    before = text[max(0, start - 120):start].lower()
    for u, p in _UNIT_BEFORE:
        if re.search(p, before):
            return u, True
    # no unit stated: go by size (interns earn $15-150/hr, $3-15K/month, $30-300K/year)
    if value < 200:
        return "h", False
    if 1500 <= value < 20000:
        return "m", False
    if value >= 25000:
        return "y", False
    return None, False


def parse_pay(text):
    """Find the stated pay range in a posting. Returns {"lo", "hi", "unit", "text"} in dollars per hour, or None."""
    if not text:
        return None
    t = clean(text)
    found = []
    for m in _MONEY.finditer(t):
        g = m.groups()
        lo = _amount(*g[0:3]) if g[0] else _amount(*g[6:8], None)
        hi = _amount(*g[3:6]) if g[3] else None
        if lo is None or lo <= 0:
            continue
        if _NOT_PAY.search(t[max(0, m.start() - 30):m.end() + 12]) or re.match(r"\s?(?:million|billion|[mb]n?\b)", t[m.end():], re.I):
            continue
        unit, stated = _unit(t, m.start(), m.end(), lo)
        if unit is None or not (stated or _PAYWORDS.search(t[max(0, m.start() - 150):m.start()])):
            continue
        if hi is not None and hi < lo:
            hi = None
        lo_h, hi_h = lo / HOURS_PER[unit], (hi or lo) / HOURS_PER[unit]
        if not (12 <= lo_h <= 250 and hi_h <= 300):
            continue
        found.append((unit, lo_h, hi_h, m.group(0).strip(), m.start()))
    if not found:
        return None
    # postings often list one range per city right after each other ("Seattle - $X  Palo Alto - $Y"): span those,
    # but not an unrelated figure further down (a full-time range, a second program)
    unit, same = found[0][0], [found[0]]
    for f in found[1:]:
        if f[0] != unit or f[4] - same[-1][4] > 200:
            break
        same.append(f)
    return {"lo": round(min(f[1] for f in same), 2), "hi": round(max(f[2] for f in same), 2), "unit": unit,
            "text": found[0][3]}


def clean(s):
    """Text out of HTML or JSON-escaped HTML."""
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = html.unescape(html.unescape(s))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s.replace("\\n", " ").replace("\\t", " "))


def label(p):
    if not p:
        return None
    lo, hi = round(p["lo"]), round(p["hi"])
    return f"${lo}/hr" if lo == hi else f"${lo}–{hi}/hr"


def from_range(lo, hi, interval):
    """Structured ranges (Lever salaryRange, Greenhouse pay_input_ranges)."""
    unit = next((u for u, p in _UNIT_AFTER if re.search(p, interval or "", re.I)), None)
    if unit is None and lo:
        unit = _unit("", 0, 0, lo)[0]
    if not lo or unit is None:
        return None
    lo_h, hi_h = lo / HOURS_PER[unit], (hi or lo) / HOURS_PER[unit]
    if not (12 <= lo_h <= 250):
        return None
    return {"lo": round(lo_h, 2), "hi": round(hi_h, 2), "unit": unit, "text": f"{lo:,.0f}–{(hi or lo):,.0f} {interval or ''}".strip()}


def _json_ld_salary(page):
    """schema.org JobPosting baseSalary, which many career sites embed for Google Jobs."""
    for blk in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            d = json.loads(blk.strip())
        except ValueError:
            continue
        for item in d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []:
            bs = isinstance(item, dict) and item.get("baseSalary")
            if not isinstance(bs, dict) or (bs.get("currency") or "USD") != "USD":
                continue
            v = bs.get("value") or {}
            if isinstance(v, dict):
                lo, hi = v.get("minValue") or v.get("value"), v.get("maxValue")
                unit = v.get("unitText") or ""
            else:
                lo, hi, unit = v, None, ""
            try:
                p = from_range(float(lo), float(hi) if hi else None, unit)
            except (TypeError, ValueError):
                p = None
            if p:
                return p
    return None


# ---------------- per-ATS readers: each returns the posting text (or a parsed range) ----------------

async def _greenhouse(http, url, rec):
    jid = re.search(r"(?:/jobs/|[?&](?:gh_jid|token)=)(\d+)", url).group(1)
    slugs = [b.split(":", 1)[1] for b in rec.get("boards", []) if b.startswith("greenhouse:")]
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)/jobs/", url)
    if m:
        slugs.insert(0, m.group(1))
    for slug in slugs:
        d = await http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}?pay_transparency=true")
        if not isinstance(d, dict):
            continue
        for r in d.get("pay_input_ranges") or []:
            if (r.get("currency_type") or "USD") == "USD" and r.get("min_cents"):
                p = from_range(r["min_cents"] / 100, (r.get("max_cents") or 0) / 100 or None, r.get("title") or "")
                if p:
                    return p
        return parse_pay(d.get("content"))
    return None if slugs else await _page(http, url, rec)


async def _lever(http, url, rec):
    m = re.search(r"(jobs\.(?:eu\.)?lever\.co)/([^/]+)/([0-9a-f-]{36})", url)
    host = "api.eu.lever.co" if ".eu." in m.group(1) else "api.lever.co"
    d = await http.get_json(f"https://{host}/v0/postings/{m.group(2)}/{m.group(3)}")
    if not isinstance(d, dict):
        return None
    sr = d.get("salaryRange") or {}
    if sr.get("min") and (sr.get("currency") or "USD") == "USD":
        p = from_range(sr["min"], sr.get("max"), sr.get("interval") or "")
        if p:
            return p
    text = " ".join([d.get("descriptionPlain") or "", d.get("additionalPlain") or "", d.get("salaryDescriptionPlain") or ""] +
                    [(l.get("text") or "") + " " + clean(l.get("content") or "") for l in d.get("lists") or []])
    return parse_pay(text)


async def _workday(http, url, rec):
    sp = urlsplit(url)
    parts = [p for p in sp.path.split("/") if p]
    if "myworkdayjobs" not in sp.netloc or "job" not in parts:
        return await _page(http, url, rec)
    i = parts.index("job")
    site = parts[i - 1]
    tenant = sp.netloc.split(".")[0]
    d = await http.get_json(f"https://{sp.netloc}/wday/cxs/{tenant}/{site}/{'/'.join(parts[i:])}")
    if not isinstance(d, dict):
        return None
    return parse_pay((d.get("jobPostingInfo") or {}).get("jobDescription"))


async def _smartrecruiters(http, url, rec):
    m = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", url)
    d = await http.get_json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
    if not isinstance(d, dict):
        return None
    secs = ((d.get("jobAd") or {}).get("sections") or {})
    return parse_pay(" ".join((s or {}).get("text") or "" for s in secs.values() if isinstance(s, dict)))


async def _eightfold(http, url, rec, domains):
    sp = urlsplit(url)
    jid = re.search(r"/job/(\d+)", sp.path).group(1)
    domain = domains.get(sp.netloc)
    if not domain:
        return await _page(http, url, rec)
    d = await http.get_json(f"https://{sp.netloc}/api/pcsx/position_details?position_id={jid}&domain={domain}&hl=en")
    text = ((d or {}).get("data") or {}).get("jobDescription") if isinstance(d, dict) else None
    if not text:
        d = await http.get_json(f"https://{sp.netloc}/api/apply/v2/jobs/{jid}?domain={domain}")
        text = (d or {}).get("job_description") if isinstance(d, dict) else None
    return parse_pay(text)


async def _page(http, url, rec):
    r = await http.request("GET", url, headers={"Accept": "text/html,application/xhtml+xml"})
    if r is None or r.status_code != 200:
        return None
    page = r.text[:2_000_000]
    # a short link (go.vansh.nyc, Simplify) may land on an ATS with a cleaner API
    final = str(getattr(r, "url", "") or "")
    if final and final != url and _route(final) is not _page:
        return await lookup(http, dict(rec, url=final), {})
    return _json_ld_salary(page) or parse_pay(page)


def _route(url):
    if re.search(r"greenhouse\.io/.+/jobs/\d+|[?&]gh_jid=\d+", url):
        return _greenhouse
    if re.search(r"lever\.co/[^/]+/[0-9a-f-]{36}", url):
        return _lever
    if "myworkdayjobs.com" in url:
        return _workday
    if re.search(r"smartrecruiters\.com/[^/]+/\d+", url):
        return _smartrecruiters
    if re.search(r"/careers/job/\d+", url):
        return _eightfold
    return _page


async def lookup(http, rec, domains):
    fn = _route(rec["url"])
    try:
        if fn is _eightfold:
            return await fn(http, rec["url"], rec, domains)
        return await fn(http, rec["url"], rec)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


async def enrich(http, recs, cache, t0, per_run=150, recheck_days=3, domains=None):
    """Look up pay for up to per_run roles (the given order is the priority), updating cache in place."""
    todo = []
    for rec in recs:
        c = cache.get(rec["id"])
        if c and (c.get("lo") or (t0 - (parse_time(c.get("at")) or t0)) < timedelta(days=recheck_days)):
            continue
        todo.append(rec)
        if len(todo) >= per_run:
            break

    async def one(rec):
        p = await lookup(http, rec, domains or {})
        cache[rec["id"]] = dict(p or {}, at=iso(t0))
    await asyncio.gather(*(one(r) for r in todo))
    return len(todo)


def apply(rec, cache):
    """Copy a cached range onto a role: pay_hr [lo, hi] for sorting, and an hourly label."""
    c = cache.get(rec["id"]) or {}
    if not c.get("lo") and rec.get("pay"):
        c = parse_pay(rec["pay"]) or {}   # Ashby lists pay inline
    if c.get("lo"):
        rec["pay_hr"] = [c["lo"], c["hi"]]
        rec["pay_label"] = label(c)
        if not rec.get("pay"):
            rec["pay"] = c.get("text")
