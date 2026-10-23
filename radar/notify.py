"""Phone alerts. ntfy (free, no account: install the ntfy app and subscribe to your topic) and/or a Discord webhook."""
import os

import httpx

from .util import parse_time, now_utc

MAX_SINGLE = 12


def ago(iso_s):
    t = parse_time(iso_s)
    if not t:
        return "date not shown"
    h = (now_utc() - t).total_seconds() / 3600
    if h < 1:
        return f"{max(1, int(h * 60))} min ago"
    if h < 48:
        return f"{int(h)} h ago"
    return f"{int(h // 24)} days ago"


def line(j):
    bits = [j.get("term") or "", j.get("location") or "", f"posted {ago(j.get('posted_at') or j.get('agg_posted_at'))}"]
    if j.get("pay"):
        bits.append(j["pay"])
    if j.get("yc"):
        bits.append(f"YC {j['yc']}")
    return " · ".join(b for b in bits if b)


async def notify(jobs, board_url=None):
    jobs = sorted(jobs, key=lambda j: (not j.get("watchlist"), -j.get("score", 0)))
    topic = os.environ.get("NTFY_TOPIC")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    hook = os.environ.get("DISCORD_WEBHOOK")
    async with httpx.AsyncClient(timeout=15) as c:
        if topic:
            for j in jobs[:MAX_SINGLE]:
                big = j.get("watchlist")
                pr = 5 if big else (4 if j.get("score", 0) >= 5 else 3)
                try:
                    await c.post(server, json={
                        "topic": topic,
                        "title": f"{'BIG TECH' if big else 'Startup'}: {j['company']} – {j['title']}"[:250],
                        "message": line(j),
                        "priority": pr,
                        "click": j["url"],
                        "tags": ["rotating_light" if big else "rocket"],
                        "actions": [{"action": "view", "label": "Apply", "url": j["url"]}] +
                                   ([{"action": "view", "label": "Board", "url": board_url}] if board_url else []),
                    })
                except httpx.HTTPError as e:
                    print("ntfy failed:", e)
            if len(jobs) > MAX_SINGLE:
                rest = jobs[MAX_SINGLE:]
                await c.post(server, json={
                    "topic": topic, "title": f"+{len(rest)} more new intern roles", "priority": 3,
                    "message": "\n".join(f"{j['company']} – {j['title']}" for j in rest[:30]),
                    **({"click": board_url} if board_url else {}),
                })
        if hook:
            chunks = [jobs[i:i + 10] for i in range(0, len(jobs), 10)]
            for ch in chunks:
                embeds = [{"title": f"{j['company']} – {j['title']}"[:250], "url": j["url"], "description": line(j)[:400],
                           "color": 0xE5484D if j.get("watchlist") else 0x3E63DD} for j in ch]
                try:
                    await c.post(hook, json={"content": f"{len(ch)} new intern role(s)", "embeds": embeds})
                except httpx.HTTPError as e:
                    print("discord failed:", e)
    if not topic and not hook:
        print("No NTFY_TOPIC or DISCORD_WEBHOOK set; skipping alerts.")
