"""Shared, conservative traffic policy for all Travsport management commands.

The PostgreSQL lock and checkpoints also cover disposable Fly machines. There
are deliberately no proxy, identity rotation, CAPTCHA or block bypass options.
"""

import asyncio
import logging
import math
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import psycopg
from django.conf import settings
from django.core.management.base import CommandError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from psycopg.types.json import Jsonb

from scraper.robots import RobotsRules

LOG = logging.getLogger(__name__)
DAY = 24 * 60 * 60
LOCK_ID = 724_193_658_103
STATE_KEY = "travsport"
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "websocket", "eventsource"}
BLOCKED_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "facebook.net", "hotjar.com",
)
CHALLENGE_TEXT = (
    "threat protection - request blocked", "too many requests",
    "verify you are human", "verify that you are human", "access denied",
    "your ip has been blocked", "your ip address has been blocked",
    "bekräfta att du är människa", "kontrollera att du är människa",
)


class ScrapeStopped(CommandError):
    """A stop must escape every command's normal per-page error handling."""


def _number(name, default, minimum, maximum=None):
    try:
        value = float(os.environ.get(name, default))
    except ValueError as exc:
        raise CommandError(f"{name} must be a number.") from exc
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise CommandError(f"{name} must be between {minimum} and {maximum or 'infinity'}.")
    return value


def _travsport(url):
    host = (urlsplit(url).hostname or "").lower()
    return host == "travsport.se" or host.endswith(".travsport.se")


def _retry_after(value):
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0, stamp.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class ScrapePolicy:
    def __init__(self):
        self.page_min = _number("TRAVSPORT_PAGE_DELAY_MIN", 30, 30)
        self.page_max = _number("TRAVSPORT_PAGE_DELAY_MAX", 60, self.page_min)
        self.request_min = _number("TRAVSPORT_REQUEST_DELAY_MIN", 2, 2)
        self.request_max = _number("TRAVSPORT_REQUEST_DELAY_MAX", 5, self.request_min)
        self.max_pages = int(_number("TRAVSPORT_MAX_PAGES_PER_RUN", 60, 1, 60))
        self.max_daily_pages = int(_number("TRAVSPORT_MAX_PAGES_PER_DAY", 100, 1, 100))
        self.max_daily_requests = int(_number("TRAVSPORT_MAX_REQUESTS_PER_DAY", 1500, 1, 1500))
        self._conn = None
        self.state = {}
        self._stopped = None
        self._failed_pages = {}
        self._pages = 0
        self._closing = False
        self._request_lock = None
        self._robots_lock = None
        self._robots = {}
        self._user_agent = "*"
        self._network_lock = None
        self._assets = {}
        self._asset_bytes = 0
        self._page_active = False
        self._generation = 0
        self._context = None

    def __enter__(self):
        db = settings.DATABASES["default"]
        if "postgresql" not in db["ENGINE"]:
            raise ScrapeStopped("The shared scraping lock requires PostgreSQL.")
        # A dedicated connection owns the advisory lock for the entire command.
        # It is never automatically reconnected: losing it must stop scraping.
        params = dict(db.get("OPTIONS", {}))
        params.update(
            dbname=db["NAME"], host=db.get("HOST") or None,
            port=db.get("PORT") or None, user=db.get("USER") or None,
            password=db.get("PASSWORD") or None, connect_timeout=15,
            application_name="travsport-polite-scraper", autocommit=True,
        )
        try:
            self._conn = psycopg.connect(**params)
            self._conn.execute("SET statement_timeout = '15s'")
            acquired = self._conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
            if not acquired:
                raise ScrapeStopped("Another Travsport scraper is running. Try again after it finishes.")
            self._conn.execute(
                "INSERT INTO scraper_crawl_state (key, data) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING", (STATE_KEY, Jsonb({})),
            )
            self.state = self._conn.execute(
                "SELECT data FROM scraper_crawl_state WHERE key = %s", (STATE_KEY,),
            ).fetchone()[0]
            self._validate_state()
            self._prune()
            self.check()
            self._save()
        except psycopg.errors.UndefinedTable as exc:
            self.__exit__(None, None, None)
            raise ScrapeStopped("Scraper state is missing. Run python manage.py migrate before scraping.") from exc
        except psycopg.Error as exc:
            self.__exit__(None, None, None)
            raise ScrapeStopped("Cannot acquire/read the shared scraper state; no scraping started.") from exc
        except BaseException:
            self.__exit__(None, None, None)
            raise
        LOG.info(
            "Polite scraping: pages %.0f-%.0fs apart; requests %.0f-%.0fs apart; "
            "limit %d pages/run, %d pages and %d requests/rolling 24h.",
            self.page_min, self.page_max, self.request_min, self.request_max,
            self.max_pages, self.max_daily_pages, self.max_daily_requests,
        )
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._conn is not None:
            # Closing the session releases its PostgreSQL advisory lock.
            self._conn.close()
            self._conn = None

    def _validate_state(self):
        try:
            if not isinstance(self.state, dict):
                raise ValueError
            for key in ("pages", "requests"):
                values = self.state.setdefault(key, [])
                if not isinstance(values, list) or any(
                    not isinstance(v, (int, float)) or not math.isfinite(v) for v in values
                ):
                    raise ValueError
            for key in ("processed", "cache"):
                if not isinstance(self.state.setdefault(key, {}), dict):
                    raise ValueError
            for key in ("cooldown_until", "next_page_at", "next_request_at", "failures", "pages_since_break"):
                value = self.state.setdefault(key, 0)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError
            if any(not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in self.state["processed"].values()):
                raise ValueError
            for entry in self.state["cache"].values():
                if (not isinstance(entry, dict) or not isinstance(entry.get("at"), (int, float))
                        or not math.isfinite(entry["at"])):
                    raise ValueError
        except (ValueError, TypeError):
            raise ScrapeStopped("Shared scraping state is invalid; stopped rather than resetting its limits.")

    def _prune(self):
        now = time.time()
        for key in ("pages", "requests"):
            self.state[key] = [stamp for stamp in self.state[key] if stamp > now - DAY]
        self.state["processed"] = {
            url: stamp for url, stamp in self.state["processed"].items() if stamp > now - 7 * DAY
        }
        self.state["cache"] = {
            key: entry for key, entry in self.state["cache"].items() if entry["at"] > now - 2 * DAY
        }

    def _save(self):
        try:
            if self._conn is None or self._conn.closed:
                self._stopped = "The shared scraper lock was lost. Stopping."
                raise ScrapeStopped("The shared scraper lock was lost. Stopping.")
            updated = self._conn.execute(
                "UPDATE scraper_crawl_state SET data = %s WHERE key = %s",
                (Jsonb(self.state), STATE_KEY),
            )
            if updated.rowcount != 1:
                self._stopped = "Shared scraper state was removed during this run. Stopping."
                raise ScrapeStopped("Shared scraper state was removed during this run. Stopping.")
        except psycopg.Error as exc:
            self._stopped = "Cannot save shared limits/progress. Stopping to avoid untracked requests."
            raise ScrapeStopped(self._stopped) from exc

    def check(self):
        if self._stopped:
            raise ScrapeStopped(self._stopped)
        until = self.state.get("cooldown_until", 0)
        if until > time.time():
            when = datetime.fromtimestamp(until, timezone.utc).isoformat()
            raise ScrapeStopped(f"Scraping paused until {when}: {self.state.get('cooldown_reason', 'cooldown')}.")

    def _require_complete(self):
        # A later successful page resets the consecutive-failure counter, but
        # must not hide an earlier failed page from shell loops or scrape_daily.
        if self._failed_pages:
            failed_urls = ", ".join(list(self._failed_pages)[:3])
            if len(self._failed_pages) > 3:
                failed_urls += ", ..."
            raise CommandError(
                f"Scrape incomplete: {len(self._failed_pages)} page(s) failed. "
                f"Failed URLs: {failed_urls}. Successfully saved pages have been kept."
            )

    def _stop(self, reason, seconds=DAY):
        self.state["cooldown_until"] = max(self.state.get("cooldown_until", 0), time.time() + seconds)
        self.state["cooldown_reason"] = reason
        when = datetime.fromtimestamp(self.state["cooldown_until"], timezone.utc).isoformat()
        self._stopped = f"{reason}. Scraping paused until {when}"
        self._save()
        LOG.error("Stopping scraper: %s. Cooldown saved for future runs.", reason)

    def _status(self, status, headers, url):
        delay = _retry_after(headers.get("retry-after"))
        if status in (401, 403, 429):
            self._stop(f"Travsport returned HTTP {status} for {url}", max(DAY, delay))
        elif status >= 500:
            self._stop(f"Travsport returned HTTP {status} for {url}", max(30 * 60, delay))
        elif delay:
            self.state["next_request_at"] = max(self.state["next_request_at"], time.time() + delay)
            self._save()

    async def _sleep_until(self, deadline):
        while time.time() < deadline:
            self.check()
            await asyncio.sleep(min(1, max(0, deadline - time.time())))
        self.check()

    async def _reserve_request(self, crawl_delay=0):
        async with self._request_lock:
            self.check()
            if self.state["requests"]:
                self.state["next_request_at"] = max(
                    self.state["next_request_at"], self.state["requests"][-1] + crawl_delay,
                )
            while self.state["next_request_at"] > time.time():
                # A failure or Retry-After can extend this while we are waiting.
                await self._sleep_until(self.state["next_request_at"])
            self._prune()
            if len(self.state["requests"]) >= self.max_daily_requests:
                wait = max(60, min(self.state["requests"]) + DAY - time.time())
                self._stop("Rolling 24-hour request budget reached", wait)
                self.check()
            now = time.time()
            self.state["requests"].append(now)
            delay = max(crawl_delay, random.uniform(self.request_min, self.request_max))
            self.state["next_request_at"] = now + delay
            # Save before allowing the request: a killed process still counts it.
            self._save()

    async def _ensure_robots(self, context, url, generation):
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        async with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
            cache_key = "robots:" + origin
            lines = self.cached(cache_key, DAY)
            if lines is None:
                await self._reserve_request()
                if self._closing or not self._page_active or generation != self._generation:
                    return None
                robots_url = origin + "/robots.txt"
                LOG.info("Checking %s", robots_url)
                try:
                    response = await context.request.get(
                        robots_url, timeout=30_000, max_redirects=0,
                        headers={"User-Agent": self._user_agent},
                    )
                    try:
                        self._status(response.status, response.headers, robots_url)
                        self.check()
                        if response.status in (404, 410):
                            lines = []
                        elif response.status == 200:
                            body = await response.text()
                            if len(body) > 512_000 or "<html" in body.lower() or "<!doctype" in body.lower():
                                self._stop("robots.txt returned an unexpected document; stopping for review", DAY)
                                self.check()
                            lines = body.splitlines()
                        else:
                            self._stop(f"Cannot verify robots.txt (HTTP {response.status}); stopping", 30 * 60)
                            self.check()
                    finally:
                        await response.dispose()
                except PlaywrightError as exc:
                    self._stop("Could not fetch robots.txt; no content requests allowed", 30 * 60)
                    raise ScrapeStopped(self._stopped) from exc
                self.remember(cache_key, lines)
            parser = RobotsRules(origin + "/robots.txt")
            parser.parse(lines)
            self._robots[origin] = parser
            return parser

    def _cache_asset(self, request, headers, body):
        """Routing disables browser HTTP cache, so retain cacheable static assets."""
        if request.method != "GET" or request.resource_type not in ("script", "stylesheet"):
            return
        control = headers.get("cache-control", "").lower()
        if any(word in control for word in ("no-store", "no-cache", "private")) or "set-cookie" in headers:
            return
        if headers.get("vary", "").lower() not in ("", "accept-encoding"):
            return
        max_age = re.search(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)", control)
        if not max_age or len(body) > 4 * 1024 * 1024:
            return
        try:
            ttl = min(3600, int(max_age.group(1)) - max(0, int(headers.get("age", "0"))))
        except ValueError:
            return
        if ttl <= 0:
            return
        old = self._assets.pop(request.url, None)
        if old:
            self._asset_bytes -= len(old["body"])
        while self._assets and self._asset_bytes + len(body) > 32 * 1024 * 1024:
            self._asset_bytes -= len(self._assets.pop(next(iter(self._assets)))["body"])
        self._assets[request.url] = {
            "until": time.time() + ttl, "body": body,
            "headers": {key: value for key, value in headers.items()
                        if key not in ("content-encoding", "content-length", "transfer-encoding")},
        }
        self._asset_bytes += len(body)

    async def _route(self, route):
        request = route.request
        generation = self._generation
        host = (urlsplit(request.url).hostname or "").lower()
        if self._closing or not self._page_active:
            await route.abort()
            return
        try:
            self.check()
            if request.resource_type in BLOCKED_RESOURCE_TYPES or any(
                host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_HOSTS
            ):
                await route.abort()
                return
            async with self._network_lock:
                self.check()
                if self._closing or not self._page_active or generation != self._generation:
                    await route.abort()
                    return
                asset = self._assets.get(request.url)
                if (request.method == "GET" and request.resource_type in ("script", "stylesheet")
                        and asset and asset["until"] > time.time()):
                    await route.fulfill(status=200, headers=asset["headers"], body=asset["body"])
                    return
                crawl_delay = 0
                if _travsport(request.url):
                    parser = await self._ensure_robots(self._context, request.url, generation)
                    if parser is None:
                        await route.abort()
                        return
                    if not parser.can_fetch(self._user_agent, request.url):
                        self._stop(f"robots.txt disallows {request.url}")
                        self.check()
                    crawl_delay = parser.crawl_delay(self._user_agent) or 0
                    rate = parser.request_rate(self._user_agent)
                    if rate and rate.requests > 0:
                        crawl_delay = max(crawl_delay, rate.seconds / rate.requests)
                await self._reserve_request(crawl_delay)
                if self._closing or not self._page_active or generation != self._generation:
                    await route.abort()
                    return
                response = await route.fetch(max_redirects=0, timeout=60_000)
                try:
                    self._status(response.status, response.headers, request.url)
                    self.check()
                    # Browser redirects can bypass route interception. Do not hand
                    # one back to the browser and silently follow it unpaced.
                    if 300 <= response.status < 400:
                        self._stop(f"Unexpected HTTP redirect for {request.url}; review before continuing", 60 * 60)
                        self.check()
                    if response.status == 200 and request.resource_type in ("script", "stylesheet"):
                        self._cache_asset(request, response.headers, await response.body())
                    await route.fulfill(response=response)
                finally:
                    await response.dispose()
        except ScrapeStopped as exc:
            self._stopped = str(exc)
            try:
                await route.abort()
            except PlaywrightError:
                pass
        except Exception as exc:
            if not self._closing and self._page_active and generation == self._generation:
                try:
                    self._stop(f"Network/request protection failed ({type(exc).__name__}); stopping", 30 * 60)
                except ScrapeStopped as stopped:
                    self._stopped = str(stopped)
            try:
                await route.abort()
            except PlaywrightError:
                pass

    @asynccontextmanager
    async def browser(self):
        self.check()
        self._request_lock = asyncio.Lock()
        self._robots_lock = asyncio.Lock()
        self._network_lock = asyncio.Lock()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1200},
                    locale="sv-SE", timezone_id="Europe/Stockholm", service_workers="block",
                )
                self._context = context
                try:
                    context.set_default_timeout(120_000)
                    context.set_default_navigation_timeout(300_000)
                    # Playwright 1.39 HTTP routing cannot intercept WebSockets.
                    # Batch scraping does not need live notification connections.
                    await context.add_init_script("""(() => {
                        const disabled = class {
                            constructor() { throw new Error('Live connections disabled during batch scraping'); }
                        };
                        window.WebSocket = disabled;
                        window.EventSource = disabled;
                    })();""")
                    await context.route("**/*", self._route)
                    page = await context.new_page()
                    self._user_agent = await page.evaluate("navigator.userAgent")
                    yield page
                    self.check()
                    self._require_complete()
                finally:
                    self._closing = True
                    await context.close()
            finally:
                await browser.close()

    async def goto(self, page, url):
        self.check()
        if not _travsport(url) or urlsplit(url).scheme != "https":
            raise ScrapeStopped("Only HTTPS Travsport page URLs are allowed.")
        if self._pages >= self.max_pages:
            raise ScrapeStopped(f"Per-run limit of {self.max_pages} pages reached. Saved pages will be skipped next run.")
        # Unload the old page before waiting so its polling/lazy loads cannot
        # keep making requests during a scheduled break.
        self._page_active = False
        self._generation += 1
        await page.goto("about:blank", timeout=30_000, wait_until="commit")
        self._prune()
        if len(self.state["pages"]) >= self.max_daily_pages:
            wait = max(60, min(self.state["pages"]) + DAY - time.time())
            self._stop("Rolling 24-hour page budget reached", wait)
            self.check()
        deadline = max(self.state["next_page_at"], time.time() + random.uniform(self.page_min, self.page_max))
        if self.state["pages_since_break"] >= 10:
            deadline = max(deadline, time.time() + random.uniform(300, 600))
            self.state["pages_since_break"] = 0
            LOG.info("Taking a 5-10 minute break after 10 pages.")
        self.state["next_page_at"] = deadline
        self._save()
        LOG.info("Waiting %.0fs before loading the next page.", max(0, deadline - time.time()))
        await self._sleep_until(deadline)
        self.state["pages"].append(time.time())
        self.state["pages_since_break"] += 1
        self.state["next_page_at"] = time.time() + random.uniform(self.page_min, self.page_max)
        self._pages += 1
        self._save()
        try:
            self._page_active = True
            response = await page.goto(url, timeout=300_000, wait_until="domcontentloaded")
        except PlaywrightError:
            self.check()
            raise
        if response is not None:
            self._status(response.status, response.headers, url)
        await self.check_page(page)
        return response

    async def check_page(self, page):
        self.check()
        try:
            signals = await page.evaluate("""() => ({
                title: document.title.toLowerCase(),
                text: (document.body?.innerText || '').slice(0, 3000).toLowerCase(),
                challenge: !!document.querySelector(
                    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
                    + 'iframe[src*="challenges.cloudflare.com"], #challenge-form, .g-recaptcha, .h-captcha')
            })""")
        except PlaywrightError:
            self.check()
            return
        if signals["challenge"] or any(
            phrase in signals["title"] or phrase in signals["text"] for phrase in CHALLENGE_TEXT
        ):
            self._stop("Travsport displayed an access block or human verification page")
        self.check()

    async def wait_for_selector(self, page, selector, timeout=120_000):
        deadline = time.monotonic() + timeout / 1000
        while True:
            await self.check_page(page)
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                element = await page.wait_for_selector(selector, timeout=min(1000, remaining))
                await self.check_page(page)
                return element
            except PlaywrightTimeoutError:
                await self.check_page(page)
                if time.monotonic() >= deadline:
                    raise

    async def failure(self, page, url, reason):
        self._failed_pages[url] = reason
        await self.check_page(page)
        self._page_active = False
        self._generation += 1
        await page.goto("about:blank", timeout=30_000, wait_until="commit")
        self.state["failures"] += 1
        count = self.state["failures"]
        if count >= 3:
            # The cooldown itself is the response to this streak; a subsequent
            # run after it expires gets a new, bounded opportunity.
            self.state["failures"] = 0
            self._stop("Three consecutive failed/empty pages; check the site or parser", 60 * 60)
            self.check()
        delay = random.uniform(60, 120) * (2 ** (count - 1))
        self.state["next_page_at"] = max(self.state["next_page_at"], time.time() + delay)
        self.state["next_request_at"] = max(self.state["next_request_at"], time.time() + delay)
        self._save()
        LOG.warning("Failed/empty page (%d/3): %s; waiting %.0fs. %s", count, url, delay, reason)
        await self._sleep_until(time.time() + delay)

    def success(self, url):
        self.check()
        self.state["failures"] = 0
        self.state["processed"][url] = time.time()
        self._save()
        self._failed_pages.pop(url, None)

    def is_recent(self, url, ttl_seconds):
        self.check()
        stamp = self.state["processed"].get(url, 0)
        return stamp > time.time() - ttl_seconds

    def cached(self, key, ttl_seconds):
        self.check()
        entry = self.state["cache"].get(key)
        if entry and entry["at"] > time.time() - ttl_seconds:
            return entry.get("value")
        return None

    def remember(self, key, value):
        self.check()
        self.state["cache"][key] = {"at": time.time(), "value": value}
        self._save()
