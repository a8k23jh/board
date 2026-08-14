# VC Role Tracker

Automatically tracks **Chief of Staff, BizOps, Strategy & Ops, Founder's Office,
Corp Dev, RevOps, GM, and ops-leadership roles** across the portfolio job boards
of ~19 top VC firms, and surfaces new postings within about an hour of going live.

## How it works

- **`scraper/scrape.py`** polls each firm's job board API (most are powered by
  Consider or Getro), filters titles and locations, and dedupes against
  `data/seen.json`.
- **GitHub Actions** (`.github/workflows/scrape.yml`) runs it **hourly**, commits
  the results, and — when new matching roles appear — **opens an issue** in this
  repo, which sends you an email notification within minutes of a job going live.
- **GitHub Pages** serves `docs/index.html`: a dashboard where you can filter by
  role type / metro / board, see NEW badges, and mark roles as
  Applied / Saved / Hidden (stored in your browser; exportable as CSV).

## One-time setup (~5 minutes)

1. **Create the repo.** On GitHub: *New repository* → name it (e.g.
   `vc-job-tracker`) → **Public** (required for free GitHub Pages) → Create.
2. **Add the files.** Either:
   - *Git CLI:* `git init && git add -A && git commit -m init` then push to the new repo, **or**
   - *Web only:* use *Add file → Upload files* and drag in everything **except**
     the `.github` folder, then use *Add file → Create new file*, type
     `.github/workflows/scrape.yml` as the filename, and paste in the contents of
     `WORKFLOW-COPY-scrape.yml` (an identical, visible copy included at the repo
     root — web uploads sometimes silently drop hidden dot-folders, so creating
     the workflow file manually guarantees it exists). You can delete
     `WORKFLOW-COPY-scrape.yml` afterwards.
3. **Enable the workflow.** Repo → *Actions* tab → enable workflows if prompted →
   open "Scrape VC job boards" → *Run workflow* to do the first run manually.
4. **Enable the dashboard.** Repo → *Settings → Pages* → Source: *Deploy from a
   branch* → Branch: `main`, folder `/docs` → Save. Your dashboard will be at
   `https://<username>.github.io/<repo>/` a minute or two later.
5. **Check notifications.** Make sure you're *Watching* the repo
   (Watch → Participating and @mentions is enough — issues in your own repo notify
   you) and that GitHub emails are on in your notification settings.

The first run marks everything as already-seen and sends **no** notification;
from then on, every hourly run that finds new matching roles opens an issue.

## Tuning

- **`config.json`** — add/remove title patterns (`categories`), noise filters
  (`exclude_title_pattern`), metros and city keywords, and whether to include
  remote or unknown-location roles.
- **`boards.json`** — add a board: `{"id", "name", "url", "platform"}` where
  platform is `consider` or `getro`. Most VC portfolio boards use one of the two
  (footer says "Powered by …").
- **Schedule** — edit the `cron:` line in `.github/workflows/scrape.yml`.

## If a board fails

`data/status.json` (and a warning bar on the dashboard) shows per-board health.
Boards occasionally change their APIs; run
`python scraper/scrape.py --diagnose --boards <id>` locally (or read the Action
logs) to see what the API returned, and adjust the adapter. The scraper is
deliberately defensive — one board failing never blocks the others.

## Known gaps

- **Founders Fund** has no public aggregated portfolio board; **Y Combinator's
  Work at a Startup** requires a login. Neither is included.
- LinkedIn is not scraped (against their terms of service; no stable public API).
- Boards cover what portfolio companies sync to them — a company that never lists
  a role on its investor's board won't appear.
