# Intern Radar

A personal internship board that checks company job systems directly, so it can see a posting
before aggregators like Simplify list it. It runs free on GitHub Actions every ~5 minutes, sends a
phone alert for each new matching role, and keeps a web board of every open role that fits.

## What it checks

| Source | How | Count at setup |
| --- | --- | --- |
| Ashby, Greenhouse, Lever, SmartRecruiters boards | Public JSON APIs, every run | ~2,100 companies |
| Workday sites (NVIDIA, Salesforce, Adobe, Intel, Cisco, Genentech, Snap, ...) | Workday search API, "intern" + "co-op" | ~1,770 sites |
| Amazon, Google, Apple, Microsoft, Uber, Netflix, Qualcomm | Each company's own careers search | 7 |
| Simplify + vanshb03 GitHub lists | Backstop for companies with no API (Meta, Tesla, ...) | 2 |

The company list grows by itself: once a day the `discover` job pulls every job-board link from the
Simplify internship and new-grad lists, then checks each active YC company for an Ashby, Greenhouse or
Lever board.

## Setup (about 5 minutes)

Fastest: install the GitHub CLI (`brew install gh` on a Mac), then from this folder run

```
bash setup.sh
```

It signs you in, creates the public repo, pushes, saves the alert secret, turns on the board,
starts the first runs and sends a test alert. Then install the ntfy app and subscribe to
`zheng-radar-fdfbb4f139`. The manual steps below do the same thing by hand.

### Manual setup

1. **Make a repo.** On GitHub, create a **public** repository named `intern-radar` (public repos get
   unlimited free Actions minutes; private ones run out at this cadence). Then, from this folder:
   ```
   git init -b main && git add . && git commit -m "intern radar"
   git remote add origin https://github.com/<you>/intern-radar.git
   git push -u origin main
   ```
2. **Phone alerts.** Install the free **ntfy** app (iOS / Android) and subscribe to the topic
   `zheng-radar-fdfbb4f139` (anyone who knows a topic name can read it, so keep it private).
   In the repo: Settings → Secrets and variables → Actions → **New repository secret** →
   `NTFY_TOPIC` = `zheng-radar-fdfbb4f139`. (Optional: a `DISCORD_WEBHOOK` secret also works.)
3. **Board.** Settings → Pages → Source: **GitHub Actions**. Then Actions → `pages` → Run workflow.
   Your board will be at `https://<you>.github.io/intern-radar/`. Add it as a repository
   **variable** `BOARD_URL` (same screen as secrets, Variables tab) so alerts link to it.
4. **Start it.** Actions tab → enable workflows if asked → run `discover` once, then `poll`.
   The first poll fills the board without alerting (so you don't get 2,000 pings); after that,
   every new matching role posted in the last 48 hours sends an alert.

### Optional: faster than GitHub's schedule

GitHub runs `*/5` schedules late when it's busy (gaps of 10–15 minutes are common). For a steady
2–3 minute cadence, use a free scheduler such as cron-job.org to call:

```
POST https://api.github.com/repos/<you>/intern-radar/dispatches
Authorization: Bearer <fine-grained token with Contents: read and write on this repo only>
Accept: application/vnd.github+json
Body: {"event_type": "poll"}
```

## Tuning

- `config/profile.yaml` — what counts as a match (title patterns, excluded hardware/PhD/non-tech
  roles, US/Canada rules), the fit score, and the alert age limit.
- `config/companies.yaml` — the big-tech watchlist (score bonus, "Big tech" tag, loudest alerts),
  boards to always poll, and Workday sites with how many pages to read.
- To add a startup by hand, put its Ashby/Greenhouse/Lever slug under `boards:` (the slug is the part
  after `jobs.ashbyhq.com/`, `boards.greenhouse.io/`, or `jobs.lever.co/`).

## Files

- `data/jobs.json` — every open matching role (the board reads this).
- `data/new_log.json` — the last 500 alerts, newest first.
- `data/companies.json` — every board being polled.
- `data/state.json` — when each posting was first seen.

## Limits worth knowing

- Meta, Tesla, Atlassian, AMD, Garmin and Pinterest have no public job API that works from GitHub,
  so they come through the Simplify lists, which lag by hours.
- Apple, Microsoft and Uber use unofficial endpoints that can change; the run log lists any source
  that failed (Actions → poll → latest run).
- Scheduled workflows pause after 60 days without repo activity; the bot's commits normally count.
