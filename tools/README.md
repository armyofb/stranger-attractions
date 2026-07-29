# Event refresher

`refresh_events.py` keeps the site's show list current. It is a plain Python script —
no AI, no cloud agent, nothing to approve — run by Windows Task Scheduler on the NUC.

## Why a scheduled task and not a Claude routine

Same reasoning as the AA scraper: this job is **deterministic**. Fetch JSON, diff against
`events.js`, write, commit, push. There is no judgment call that needs a model, so putting
an LLM in the loop only adds cost, latency, and interactive approval prompts that stall
unattended runs. Scheduled task = persistence, not intelligence.

## Where the data comes from

Skeletix (his ticketing platform) is a BigCommerce storefront whose event pages render
client-side from an **Algolia** index. The script queries that index directly:

```
POST https://4P6121I2PI-dsn.algolia.net/1/indexes/event-prod/query
```

The app id and search-only API key are public values lifted from Skeletix's own page
source. The index holds only current/on-sale events, and each record is fully structured:
ISO date, doors/show times, age restriction, venue with street address, act list, ticket
price, ticket URL, and a 1280px poster URL. No HTML scraping.

Facebook is deliberately **not** consulted: every show he sells tickets for is on Skeletix,
and Facebook needs a logged-in browser session. `facebook` fields already in `events.js`
are preserved; new entries get `""` and the card simply omits the FB button.

## Behavior

- Adds any future event matching the promoter query that isn't already in `events.js`,
  downloading its poster to `assets/posters/<headliner-slug>.jpg`.
- Prunes shows more than 30 days past (and their posters).
- Re-sorts entries by date; **preserves existing entries verbatim**, so hand-written
  `tag` text and `facebook` links survive.
- Maintains two footer stamps in `events.js`:
  - `LAST_UPDATED` — bumped only when the show listings actually changed.
  - `LAST_CHECKED` — bumped on **every** successful run, so the public footer proves the
    refresher is still alive even on a quiet day. (This means a small commit + deploy on
    every run: ~2/day, versus Cloudflare Pages' 500 builds/month free allowance.)
  If either constant is missing from a hand-edited `events.js`, the script re-inserts it.
- Never deletes a *future* show, even if it disappears from the index.
- **State Street Pub is forced to `21 & Over`** — the venue is 21+ and Skeletix listings
  sometimes say "ALL AGES".
- `tag` (editorial genre/origin flavor text) is left empty for new shows; fill it in by
  hand in `events.js` if wanted.
- Logs every run to `tools/refresh.log`; shows at his venues that the query did not match
  (band-hosted events, other promoters) are listed under `POSSIBLY MISSING` for a human to
  review rather than auto-added.

## Usage

```
python tools/refresh_events.py --dry-run    # report only, change nothing
python tools/refresh_events.py --no-git     # write events.js but don't commit/push
python tools/refresh_events.py              # normal (what the scheduled task runs)
```

## Scheduled task

Registered as **StrangerAttractionsEventRefresh**, running `tools/refresh_events.bat`
daily at 10:00 and 18:00. Requires the user to be logged on (git push uses the `gh`
credential helper from the user's keyring).

```powershell
Get-ScheduledTask -TaskName StrangerAttractionsEventRefresh | Get-ScheduledTaskInfo
Start-ScheduledTask -TaskName StrangerAttractionsEventRefresh   # run it now
```
