import asyncio
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from playwright.async_api import Error as PlaywrightError

from scraper.models import HorseResult, StartList
from scraper.polite import ScrapePolicy, ScrapeStopped

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROW_SELECTOR = "div[role='row'][data-rowindex]"
LOPP_HEADER_SELECTOR = "xpath=//h2[starts-with(normalize-space(),'Lopp')]"
CALENDAR_STARTLIST_SELECTOR = "a[href*='/race/raceday/ts'][href*='/startlist']"
STOCKHOLM_TZ = ZoneInfo("Europe/Stockholm")
SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}
SWEDISH_MONTH_BY_NUMBER = {v: k.lower() for k, v in SWEDISH_MONTH.items()}
CALENDAR_URL = "https://sportapp.travsport.se/race/calendar/race?year={year}&competition=SPORT&month={month:02d}"


def swedish_date_to_yyyymmdd(txt: str) -> str:
    p = (txt or "").strip().upper().split()
    d, m, y = (p[1], p[2], p[3]) if len(p) == 4 else p
    return f"{int(y):04d}{SWEDISH_MONTH[m]:02d}{int(d):02d}"

def normalize_cell_text(s: str) -> str:
    if s is None:
        return ""
    return s.replace("\u00a0", " ").strip()

def trim_to_max(s: str, max_len: int) -> str:
    s = s or ""
    return s if len(s) <= max_len else s[:max_len]

_paren_re = re.compile(r"\([^)]*\)")

def normalize_startlista_name(name: str) -> str:
    cleaned = normalize_cell_text(name)

    cleaned = cleaned.replace("*", "")
    cleaned = cleaned.replace("'", "").replace("’", "")
    cleaned = _paren_re.sub("", cleaned)

    if len(cleaned) >= 7:
        cleaned = cleaned[:-7]

    cleaned = cleaned.rstrip()
    cleaned = cleaned.upper()

    return trim_to_max(cleaned, 50)

def normalize_kusk(kusk: str, max_len: int) -> str:
    cleaned = re.sub(r"\s+", " ", normalize_cell_text(kusk)).strip()
    return trim_to_max(cleaned, max_len)


dist_re = re.compile(r"\s*(\d+)\s*/\s*([\d,]+)", re.I)

def parse_dist_spar(txt: str):
    t = normalize_cell_text(txt)
    m = dist_re.match(t)
    if not m:
        return None, None
    spar = int(m.group(1))
    dist = int(m.group(2).replace(",", ""))
    return dist, spar


def _strip(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

FULLNAME_TO_BANKOD = {
    "ARVIKA":"Ar","AXEVALLA":"Ax","BERGSÅKER":"B","BODEN":"Bo","BOLLNÄS":"Bs",
    "DANNERO":"D","DALA JÄRNA":"Dj","ESKILSTUNA":"E","JÄGERSRO":"J","FÄRJESTAD":"F",
    "GÄVLE":"G","GÖTEBORG TRAV":"Gt","HAGMYREN":"H","HALMSTAD":"Hd","HOTING":"Hg",
    "KARLSHAMN":"Kh","KALMAR":"Kr","LINDESBERG":"L","LYCKSELE":"Ly","MANTORP":"Mp",
    "OVIKEN":"Ov","ROMME":"Ro","RÄTTVIK":"Rä","SOLVALLA":"S","SKELLEFTEÅ":"Sk",
    "SOLÄNGET":"Sä","TINGSRYD":"Ti","TÄBY TRAV":"Tt","UMÅKER":"U","VEMDALEN":"Vd",
    "VAGGERYD":"Vg","VISBY":"Vi","ÅBY":"Å","ÅMÅL":"Åm","ÅRJÄNG":"År",
    "ÖREBRO":"Ö","ÖSTERSUND":"Ös","BJÄRKE":"Bj",
}
FULLNAME_TO_BANKOD |= {_strip(k): v for k, v in FULLNAME_TO_BANKOD.items()}

def track_to_bankod(n: str) -> str:
    n = normalize_cell_text(n).upper().strip()  
    n = _strip_nav_prefixes(n)  
    if not n:  
        return ""  

    hit = FULLNAME_TO_BANKOD.get(n)  
    if hit:  
        return hit  

    n_ascii = _strip(n)  
    hit2 = FULLNAME_TO_BANKOD.get(n_ascii)  
    if hit2:  
        return hit2  

    fallback = n[:2].title()  
    logging.warning("Unknown track name=%r (ascii=%r). Using fallback bankod=%r", n, n_ascii, fallback)  
    return fallback  

NAV_PREFIXES = (  
    "TÄVLINGSDAGSRESULTAT",  
    "DAGSRESULTAT",          
    "STARTLISTA",            
    "TÄVLINGSDAG",           
    "TRAVTÄVLING",           
    "DAG",                   
)  

def _strip_nav_prefixes(up: str) -> str:  
    s = (up or "").strip()  
    changed = True  
    while changed and s:  
        changed = False  
        for p in NAV_PREFIXES:  
            if s == p:  
                s = ""  
                changed = True  
                break  
            if s.startswith(p + " "):  
                s = s[len(p):].strip()  
                changed = True  
                break  
    return s  


MONTHS_PATTERN = "|".join(SWEDISH_MONTH.keys())  
DATE_PART_RX = re.compile(rf"\b(\d{{1,2}})\s+({MONTHS_PATTERN})\s+(\d{{4}})\b", re.I)  
WEEKDAYS = ("MÅNDAG","TISDAG","ONSDAG","TORSDAG","FREDAG","LÖRDAG","SÖNDAG")  
WEEKDAYS_RX = re.compile(rf"\b(?:{'|'.join(WEEKDAYS)})\b", re.I)  

async def _get_nav_texts(page):  
    for sel in ("[class*='RaceDayNavigator'] span", "header span"):  
        loc = page.locator(sel)  
        n = await loc.count()  
        texts = []  
        for i in range(n):  
            t = normalize_cell_text(await loc.nth(i).inner_text())  
            if t:  
                texts.append(t)  
        if texts:  
            return texts  
    return []  

def _extract_track_and_date(texts):
    cleaned = [t for t in (texts or []) if t and t.strip()]  
    cleaned = [t.strip() for t in cleaned]  

    date_container = None  
    date_part = None  

    for t in cleaned:  
        m = DATE_PART_RX.search(t.upper())  
        if m:  
            date_container = t  
            date_part = m.group(0).upper()  
            break  

    if not date_part:  
        return None, None  

    track_txt = None  
    for t in cleaned:  
        if t == date_container:  
            continue  
        if re.search(r"\d", t):  
            continue  

        up = t.upper().strip()  
        up = _strip_nav_prefixes(up)  
        if not up:  
            continue  

        if any(up == p or up.startswith(p + " ") for p in NAV_PREFIXES):  
            continue  

        track_txt = up  
        break  

    if not track_txt and date_container:  
        up = date_container.upper()  
        up = up.replace(date_part, " ")  
        up = WEEKDAYS_RX.sub(" ", up)  
        up = re.sub(r"\s+", " ", up).strip()  

        up = _strip_nav_prefixes(up)  
        track_txt = re.sub(r"\s+", " ", up).strip()  

    return track_txt, date_part  

@dataclass
class StartRow:
    startdatum: int
    bankod: str
    lopp: int
    nr: int
    namn: str
    spar: int | None
    distans: int | None
    kusk: str
    struken: bool


async def scrape_startlist(page, policy: ScrapePolicy, url: str) -> list[StartRow]:
    await policy.goto(page, url)
    await policy.wait_for_selector(page, ROW_SELECTOR, timeout=120_000)
    await policy.wait_for_selector(page, LOPP_HEADER_SELECTOR, timeout=120_000)

    texts = await _get_nav_texts(page)
    raw_track, date_txt = _extract_track_and_date(texts)
    if not raw_track or not date_txt:
        await policy.check_page(page)
        return []

    bankod = track_to_bankod(raw_track)
    startdatum = int(swedish_date_to_yyyymmdd(date_txt))

    out: list[StartRow] = []
    lopp_headers = page.locator("//h2[starts-with(normalize-space(),'Lopp')]")
    for i in range(await lopp_headers.count()):
        header = lopp_headers.nth(i)
        await header.scroll_into_view_if_needed()

        m = re.search(r"Lopp\s+(\d+)", normalize_cell_text(await header.inner_text()))
        if not m:
            continue
        lopp_nr = int(m.group(1))

        grid = header.locator("xpath=following::div[contains(@class,'MuiDataGrid-root')][1]")
        rows = await grid.locator("div[role='row'][data-rowindex]").all()
        if not rows:
            logging.info("Lopp %s: inga rader, hoppar över", lopp_nr)
            continue

        for row in rows:
            cell = lambda f: row.locator(f"div[data-field='{f}']")

            # Startlista använder mobilehorse??????????????
            horse_cell = cell("mobilehorse")
            if await horse_cell.count() == 0:
                horse_cell = cell("horse")

            is_struken = (await horse_cell.locator("[class*='linethrough']").count()) > 0

            nr = None
            try:
                nr_txt = normalize_cell_text(await horse_cell.locator("div").first.inner_text())
                nr_m = re.search(r"\d+", nr_txt)
                if nr_m:
                    nr = int(nr_m.group(0))
            except (PlaywrightError, ValueError):
                nr = None

            if nr is None:
                horse_text = normalize_cell_text(await horse_cell.inner_text())
                nr_m = re.search(r"\b(\d{1,2})\b", horse_text)
                if not nr_m:
                    continue
                nr = int(nr_m.group(1))

            namn_raw = ""
            if await horse_cell.locator("span").count() > 0:
                namn_raw = normalize_cell_text(await horse_cell.locator("span").first.inner_text())
            if not namn_raw:
                horse_text = normalize_cell_text(await horse_cell.inner_text())
                namn_raw = re.sub(r"^\s*\d+\s*", "", horse_text).strip()

            namn = normalize_startlista_name(namn_raw)

            kusk_raw = normalize_cell_text(await cell("driver").inner_text())
            kusk = normalize_kusk(kusk_raw, 120)

            dist_raw = normalize_cell_text(await cell("trackName").inner_text())
            distans, spar = parse_dist_spar(dist_raw)

            out.append(StartRow(
                startdatum=startdatum,
                bankod=bankod,
                lopp=lopp_nr,
                nr=nr,
                namn=namn,
                spar=spar,
                distans=distans,
                kusk=kusk,
                struken=is_struken,
            ))

    await policy.check_page(page)
    return out


def _today_yyyymmdd() -> int:
    d: date = timezone.localdate(timezone=STOCKHOLM_TZ)
    return d.year * 10000 + d.month * 100 + d.day


def _parse_ts_id(value: str) -> int:
    return int(str(value).replace("_", ""))


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}. Use YYYY-MM-DD.") from exc


def _format_ts_id(ts_id: int) -> str:
    s = str(ts_id)
    return f"{s[:-3]}_{s[-3:]}" if len(s) > 3 else s


def _startlist_ts_id_from_href(href: str) -> int | None:
    m = re.search(r"/race/raceday/ts(\d+)/startlist", href or "")
    return int(m.group(1)) if m else None


async def find_startlist_ts_ids(page, policy: ScrapePolicy, first_day: date, last_day: date) -> list[int]:
    """Use only published links for the requested dates; never guess IDs by default."""
    month = first_day.replace(day=1)
    ids = set()
    while month <= last_day:
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        from_day = max(first_day, month)
        through_day = min(last_day, next_month - timedelta(days=1))
        calendar_url = CALENDAR_URL.format(year=month.year, month=month.month)
        cache_key = f"startlist-calendar:{from_day.isoformat()}:{through_day.isoformat()}"
        cached_ids = policy.cached(cache_key, ttl_seconds=6 * 60 * 60)
        if cached_ids:
            ids.update(int(value) for value in cached_ids)
            month = next_month
            continue

        found = set()
        try:
            logging.info("Reading Startlista calendar for %s through %s", from_day, through_day)
            await policy.goto(page, calendar_url)
            await policy.wait_for_selector(page, "h2", timeout=120_000)
            await policy.wait_for_selector(page, CALENDAR_STARTLIST_SELECTOR, timeout=120_000)
            last_snapshot = None
            settled_samples = 0
            complete = False
            saw_date_header = False
            for _ in range(40):
                policy.check()
                result = await page.evaluate(
                    r"""
                    ({ firstDay, lastDay, monthName }) => {
                        const normalize = (value) => (value || "").replace(/\s+/g, " ").trim().toLowerCase();
                        const pattern = new RegExp(`(?:^|\\D)(\\d{1,2})\\s+${monthName}(?:\\b|$)`, "i");
                        const headers = Array.from(document.querySelectorAll("h2"));
                        const links = Array.from(document.querySelectorAll("a[href*='/race/raceday/ts'][href*='/startlist']"));
                        const before = (left, right) => Boolean(left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING);
                        const hrefs = [];
                        let maxDay = 0;
                        for (let index = 0; index < headers.length; index++) {
                            const match = normalize(headers[index].textContent).match(pattern);
                            if (!match) continue;
                            const day = Number(match[1]);
                            maxDay = Math.max(maxDay, day);
                            if (day < firstDay || day > lastDay) continue;
                            const nextHeader = headers[index + 1] || null;
                            for (const link of links) {
                                if (before(headers[index], link) && (!nextHeader || before(link, nextHeader))) {
                                    hrefs.push(link.getAttribute("href"));
                                }
                            }
                        }
                        const scroller = document.scrollingElement || document.documentElement;
                        const loading = Array.from(document.querySelectorAll(
                            "[role='progressbar'], [aria-busy='true'], .MuiCircularProgress-root, .MuiLinearProgress-root"
                        )).some((element) => element.getClientRects().length > 0 &&
                            window.getComputedStyle(element).visibility !== "hidden");
                        return {
                            hrefs,
                            maxDay,
                            loading,
                            atBottom: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4,
                            position: `${scroller.scrollTop}:${scroller.scrollHeight}:${scroller.clientHeight}`,
                        };
                    }
                    """,
                    {"firstDay": from_day.day, "lastDay": through_day.day,
                     "monthName": SWEDISH_MONTH_BY_NUMBER[month.month]},
                )
                for href in result["hrefs"]:
                    ts_id = _startlist_ts_id_from_href(href)
                    if ts_id is not None:
                        found.add(ts_id)
                saw_date_header = saw_date_header or result["maxDay"] > 0
                # Date headings can render before their links. Only accept three
                # settled samples at the bottom with no visible loading indicator.
                snapshot = (result["position"], tuple(sorted(found)))
                if result["atBottom"] and not result["loading"]:
                    settled_samples = settled_samples + 1 if snapshot == last_snapshot else 1
                else:
                    settled_samples = 0
                if settled_samples >= 3:
                    complete = True
                    break
                last_snapshot = snapshot
                if not result["atBottom"]:
                    await page.mouse.wheel(0, 1800)
                await asyncio.sleep(random.uniform(policy.request_max + 1, policy.request_max + 3))
            await policy.check_page(page)
        except ScrapeStopped:
            raise
        except Exception as exc:
            logging.warning("Calendar lookup failed (%s).", type(exc).__name__)
            await policy.failure(page, calendar_url, "calendar lookup failed")
            month = next_month
            continue
        if not complete:
            await policy.failure(page, calendar_url, "calendar did not finish loading within the discovery limit")
        elif not saw_date_header:
            await policy.failure(page, calendar_url, "calendar has no recognizable date headers")
        elif not found:
            await policy.failure(page, calendar_url, "calendar has no startlist links for the requested dates")
        else:
            policy.remember(cache_key, sorted(found))
            policy.success(calendar_url)
            ids.update(found)
        month = next_month
    return sorted(ids)


def upsert_resultat_from_startrow(r: StartRow):
    namn_clean = r.namn
    kusk_res = normalize_kusk(r.kusk, 80)

    desired_placering = 99 if r.struken else 0

    obj, created = HorseResult.objects.get_or_create(
        datum=r.startdatum,
        bankod=r.bankod,
        lopp=r.lopp,
        namn=namn_clean,
        defaults=dict(
            nr=r.nr,
            distans=r.distans,
            spar=r.spar,
            kusk=kusk_res,
            placering=desired_placering,
        ),
    )

    if created:
        return

    changed_fields = []

    if obj.nr != r.nr:
        obj.nr = r.nr
        changed_fields.append("nr")

    if r.distans is not None and obj.distans != r.distans:
        obj.distans = r.distans
        changed_fields.append("distans")

    if r.spar is not None and obj.spar != r.spar:
        obj.spar = r.spar
        changed_fields.append("spar")

    if kusk_res and obj.kusk != kusk_res:
        obj.kusk = kusk_res
        changed_fields.append("kusk")

    if obj.placering is None or obj.placering in (0, 99):
        if obj.placering != desired_placering:
            obj.placering = desired_placering
            changed_fields.append("placering")

    if changed_fields:
        obj.save(update_fields=changed_fields)


class Command(BaseCommand):
    help = "Scrape linked Startlista pages from the calendar with shared pacing and caching."

    START_ID = 616_280
    END_ID = 616_330

    def add_arguments(self, parser):
        parser.add_argument(
            "--manual-ids", action="store_true",
            help="Explicitly scan the hard-coded START_ID and END_ID range (subject to request limits).",
        )
        parser.add_argument("--start-id", type=_parse_ts_id, help="Manual first raceday ts-ID, for example 616_280.")
        parser.add_argument("--end-id", type=_parse_ts_id, help="Manual last raceday ts-ID, for example 616_300.")
        parser.add_argument(
            "--ids-after-start", type=int, default=None,
            help="Explicitly scan this many IDs after the first linked/manual ID. Manual --start-id alone processes one page.",
        )
        parser.add_argument(
            "--calendar-date", type=_parse_iso_date,
            help="Read linked startlists for this date and the following 5 days; defaults to yesterday. Use YYYY-MM-DD.",
        )

    def handle(self, *args, **kwargs):
        count = kwargs.get("ids_after_start")
        if count is not None and count < 0:
            raise CommandError("--ids-after-start must be 0 or greater.")
        start_id = kwargs.get("start_id")
        end_id = kwargs.get("end_id")
        if end_id is not None and start_id is None:
            raise CommandError("--end-id requires --start-id.")
        if start_id is not None and start_id <= 0:
            raise CommandError("--start-id must be positive.")
        if start_id is not None and end_id is not None and end_id < start_id:
            raise CommandError("--end-id must be greater than or equal to --start-id.")
        with ScrapePolicy() as policy:
            asyncio.run(self._scrape(policy, kwargs))

    async def _resolve_ids(self, page, policy, opts):
        count = opts.get("ids_after_start")
        start_id = opts.get("start_id")
        if start_id is not None:
            end_id = opts.get("end_id")
            if end_id is None:
                end_id = start_id + (0 if count is None else count)
            return range(start_id, end_id + 1)
        if opts.get("manual_ids"):
            return range(self.START_ID, self.END_ID + 1)

        target_day = opts.get("calendar_date") or (
            timezone.localdate(timezone=STOCKHOLM_TZ) - timedelta(days=1)
        )
        last_day = target_day if count is not None else target_day + timedelta(days=5)
        linked_ids = await find_startlist_ts_ids(page, policy, target_day, last_day)
        if count is not None:
            if not linked_ids:
                raise CommandError(f"Could not find a Startlista link for {target_day.isoformat()} in the race calendar.")
            return range(linked_ids[0], linked_ids[0] + count + 1)
        return linked_ids

    @transaction.atomic
    def _save_rows(self, rows, today_int):
        resultat_count = 0
        for row in rows:
            StartList.objects.update_or_create(
                startdatum=row.startdatum, bankod=row.bankod, lopp=row.lopp, nr=row.nr,
                defaults=dict(
                    namn=row.namn, spar=row.spar, distans=row.distans,
                    kusk=normalize_kusk(row.kusk, 120),
                ),
            )
            if row.startdatum >= today_int:
                upsert_resultat_from_startrow(row)
                resultat_count += 1
        return resultat_count

    async def _scrape(self, policy, opts):
        base = "https://sportapp.travsport.se/race/raceday/ts{}/startlist/all"
        total = 0
        total_resultat = 0
        skipped = 0
        today_int = _today_yyyymmdd()
        async with policy.browser() as page:
            ids = await self._resolve_ids(page, policy, opts)
            logging.info("Selected %d startlist pages.", len(ids))
            for ts_id in ids:
                policy.check()
                url = base.format(ts_id)
                if policy.is_recent(url, ttl_seconds=6 * 60 * 60):
                    skipped += 1
                    continue
                logging.info("Scraping %s", url)
                try:
                    rows = await scrape_startlist(page, policy, url)
                except ScrapeStopped:
                    raise
                except Exception as exc:
                    logging.warning("Startlist failed (%s).", type(exc).__name__)
                    await policy.failure(page, url, "startlist navigation or parsing failed")
                    continue
                if not rows:
                    await policy.failure(page, url, "startlist had no usable rows")
                    continue
                # Database errors stop the command instead of fetching further pages.
                resultat_count = await asyncio.to_thread(self._save_rows, rows, today_int)
                policy.success(url)
                total += len(rows)
                total_resultat += resultat_count
                logging.info("Saved %d startlista rows and %d resultat upserts.", len(rows), resultat_count)
        self.stdout.write(self.style.SUCCESS(
            f"Done. {total} startlista rows processed. {total_resultat} resultat upserts "
            f"(today/future only). {skipped} recently processed pages skipped."
        ))
