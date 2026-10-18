# Conservative Travsport scraping

All three scraper commands use `scraper/polite.py`. These settings reduce traffic;
they are not a rate approved by Svensk Travsport and cannot guarantee that an IP
will not be blocked. If access is still blocked, contact Svensk Travsport about
permission or an appropriate data feed before resuming.

## Before the next run

Apply the new state-table migration once, using the same database as the scraper:

```powershell
python manage.py migrate
```

The scraper refuses to send requests if it cannot read/write its state or acquire
its PostgreSQL advisory lock. Use a direct PostgreSQL connection (or a pooler in
session mode); transaction pooling does not preserve a session advisory lock.
All installations sharing a scraping workload must use this same database. The
lock cannot coordinate unrelated programs or installations using another database.

## Default behavior

| Protection | Default |
| --- | --- |
| Pause before loading each page, including the first | Random 30–60 seconds |
| Pause between outgoing HTTP requests | Random 2–5 seconds, one request in flight |
| Longer break | Random 5–10 minutes after every 10 pages |
| Page budget per command | 60, including calendar and discovery pages |
| Shared rolling 24-hour budgets | 100 pages and 1,500 HTTP requests |
| Recently saved startlists / results / propositions | Skip for 6 / 24 / 12 hours |
| Discovered calendar/proposition links | Cache for 6 hours after a complete scan |
| HTTP 401, 403, 429 or visible human-verification/block page | Stop; at least 24-hour cooldown |
| HTTP 5xx / network failure | Stop; at least 30-minute cooldown |
| Unexpected redirects | Stop; one-hour cooldown, review the URL/site before resuming |
| Failed or empty parsed pages | Increasing random backoff; stop after 3 consecutive failures for one hour |

`Retry-After` is honored, including HTTP dates; a longer server delay always wins.
Cooldowns, budgets, page spacing, and successful progress survive restarts and
disposable Fly machines because they live in PostgreSQL, not the container's
filesystem. Budget and block stops exit with an error rather than claiming the
whole scrape succeeded. The next permitted run skips recently completed pages.
Do not delete state to get around a cooldown.

Each command reuses one browser/page and checks robots.txt for Travsport origins.
An unavailable or unexpected robots response stops the run; a 404/410 means no
robots file was published. Longer crawl delays also apply to requests. Images,
fonts, media and known analytics hosts are blocked. Reusable JavaScript and CSS
are cached in memory only when response cache directives permit it; dynamic
race data is not served from that asset cache. Live WebSocket/EventSource
connections are disabled. Old pages are unloaded during breaks to stop polling.
There are no automatic retries of denied requests or identity/proxy rotation.

Recently processed pages can be stale until their skip period expires. In
particular, results for a day still in progress are refreshed on a later run.
Only successfully parsed pages saved to the database are marked completed;
failed/empty pages and incomplete discovery scans are not cached as successes.

## Commands

```powershell
python manage.py scrape_daily
```

This runs startlists followed by results, stopping the sequence on a safety stop.
Startlists follow published calendar links from yesterday through four days ahead.
Results follow published result links from five days ago through today. Dates
use Europe/Stockholm. Default runs no longer probe consecutive race IDs.

```powershell
python manage.py scrape_results --calendar-date 2026-10-15
python manage.py scrape_startlist --start-id 616290
python manage.py scrape_proposition --start-id 610355
```

A single `--start-id` now fetches only that raceday. `--end-id` explicitly selects
a range. Results/startlists also retain `--manual-ids` and explicit
`--ids-after-start` for intentional ranges; the shared limits still apply.
Propositions require `--start-id` instead of automatically scanning 96 IDs.

The checked-in workflow is reduced to one scheduled run daily. Its detached Fly
command reports machine startup, not final scraper completion; inspect the
scraper's logs for completion or a saved cooldown. Local edits take effect on Fly
only after you publish the updated image/workflow yourself.

## More conservative configuration

Set these in `.env` locally or in the deployment's environment. No changes to
`.env` are required to enable the defaults. You can increase delays or lower
budgets; values below the delay floors or above the budget ceilings are rejected.

| Environment variable | Default | Allowed |
| --- | --- | --- |
| `TRAVSPORT_PAGE_DELAY_MIN` | 30 | At least 30 seconds |
| `TRAVSPORT_PAGE_DELAY_MAX` | 60 | At least the configured minimum |
| `TRAVSPORT_REQUEST_DELAY_MIN` | 2 | At least 2 seconds |
| `TRAVSPORT_REQUEST_DELAY_MAX` | 5 | At least the configured minimum |
| `TRAVSPORT_MAX_PAGES_PER_RUN` | 60 | 1–60 |
| `TRAVSPORT_MAX_PAGES_PER_DAY` | 100 | 1–100 in a rolling 24 hours |
| `TRAVSPORT_MAX_REQUESTS_PER_DAY` | 1500 | 1–1500 in a rolling 24 hours |

Longer pauses can make a run take hours. Interrupting it preserves checkpoints.
No scraper, tests, builds, or migrations were run as part of these code changes.
