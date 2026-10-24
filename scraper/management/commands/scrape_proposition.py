import asyncio
import logging
import random
import re
import unicodedata
from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction
from playwright.async_api import Error as PlaywrightError

from scraper.models import Proposition
from scraper.polite import ScrapePolicy, ScrapeStopped

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}

def swedish_date_to_yyyymmdd(text: str) -> str:
    p = text.strip().upper().split()
    if len(p) == 4:
        _, d, m, y = p
    else:
        d, m, y = p
    return f"{int(y):04d}{SWEDISH_MONTH[m]:02d}{int(d):02d}"

def _strip_diacritics(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

FULLNAME_TO_BANKOD = {
    "ARVIKA":"Ar","AXEVALLA":"Ax","BERGSÅKER":"B","BODEN":"Bo","BOLLNÄS":"Bs",
    "DANNERO":"D","DALA JÄRNA":"Dj","ESKILSTUNA":"E","JÄGERSRO":"J","FÄRJESTAD":"F",
    "GÄVLE":"G","GÖTEBORG TRAV":"Gt","HAGMYREN":"H","HALMSTAD":"Hd","HOTING":"Hg",
    "KARLSHAMN":"Kh","KALMAR":"Kr","LINDESBERG":"L","LYCKSELE":"Ly","MANTORP":"Mp",
    "OVIKEN":"Ov","ROMME":"Ro","RÄTTVIK":"Rä","SOLVALLA":"S","SKELLEFTEÅ":"Sk",
    "SOLÄNGET":"Sä","TINGSRYD":"Ti","TÄBY TRAV":"Tt","UMÅKER":"U","VEMDALEN":"Vd",
    "VAGGERYD":"Vg","VISBY":"Vi","ÅBY":"Å","ÅMÅL":"Åm","ÅRJÄNG":"År","ÖREBRO":"Ö","ÖSTERSUND":"Ös",
}
_ASCII_FALLBACK = {_strip_diacritics(k): v for k, v in FULLNAME_TO_BANKOD.items()}

def track_to_bankod(name: str) -> str:
    name_up = name.strip().upper()
    if name_up.startswith(("TÄVLINGSDAG", "TRAVTÄVLING")):
        name_up = name_up.split(maxsplit=1)[1]
    if name_up in FULLNAME_TO_BANKOD:
        return FULLNAME_TO_BANKOD[name_up]
    return _ASCII_FALLBACK.get(_strip_diacritics(name_up), name_up[:2].title())

def extract_bankod_from_text(raw: str) -> str | None:
    t_up = re.sub(r"[^A-ZÅÄÖ\s]", " ", raw.upper())
    t_up = re.sub(r"\s+", " ", t_up).strip()
    for key in sorted(FULLNAME_TO_BANKOD.keys(), key=len, reverse=True):
        if key in t_up:
            return FULLNAME_TO_BANKOD[key]
    t_ascii = _strip_diacritics(t_up)
    for key in sorted(_ASCII_FALLBACK.keys(), key=len, reverse=True):
        if key in t_ascii:
            return _ASCII_FALLBACK[key]
    return None

@dataclass
class PropRow:
    startdatum: int
    bankod: str
    namn: str
    proposition: int
    distans: int | None = None
    kuskanskemal: str | None = None  

async def scrape_proposition_page(page, policy: ScrapePolicy, url: str) -> list[PropRow]:
    await policy.goto(page, url)
    await policy.wait_for_selector(page, "div[role='row'][data-rowindex]")

    bankod = None
    startdatum = None
    nav = page.locator("div[class*='RaceDayNavigator_title'] span")
    if await nav.count() >= 2:
        track_text = (await nav.nth(0).inner_text()).strip()
        bankod = extract_bankod_from_text(track_text) or track_to_bankod(track_text)
        date_text = (await nav.nth(1).inner_text()).strip()
        startdatum = int(swedish_date_to_yyyymmdd(date_text))
    if bankod is None or startdatum is None:
        nodes = page.locator("xpath=//*[self::div or self::span or self::p or self::h1 or self::h2]")
        date_re = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
        for i in range(min(await nodes.count(), 250)):
            t = (await nodes.nth(i).inner_text()).strip()
            m = date_re.search(t)
            if not m:
                continue
            date_str = m.group(1)
            startdatum = int(date_str.replace("-", ""))
            track_part = t.split(date_str)[0].strip(" •|-").strip()
            bankod = extract_bankod_from_text(track_part) or track_to_bankod(track_part)
            break

    if bankod is None or startdatum is None:
        await policy.check_page(page)
        return []

    prop_num = None
    cand = page.locator("xpath=//*[contains(normalize-space(.), 'Prop.')]")
    for i in range(min(await cand.count(), 50)):
        txt = (await cand.nth(i).inner_text()).strip()
        m = re.search(r"Prop\.\s*(\d+)", txt, flags=re.I)
        if m:
            prop_num = int(m.group(1))
            break
    if prop_num is None:
        await policy.check_page(page)
        return []

    rows = await page.locator("div[role='row'][data-rowindex]").all()
    out: list[PropRow] = []
    for row in rows:
        cell = row.locator("div[data-field='horseName'], div[data-field='horse']")
        if await cell.count() == 0:
            continue
        name_loc = cell.locator("a, span").first
        namn_raw = (await name_loc.inner_text()).strip()
        namn = namn_raw.split("(")[0].strip()
        if not namn:
            continue

        dist_val: int | None = None
        dist_cell = row.locator("div[data-field='distance']")
        if await dist_cell.count() > 0:
            dist_txt = (await dist_cell.first.inner_text()).strip()
            m = re.search(r"(\d{3,5})", dist_txt)
            if m:
                dist_val = int(m.group(1))

        kusk_pref: str | None = None
        pref_cell = row.locator("div[data-field='driverPreferences']")
        if await pref_cell.count() > 0:
            raw = (await pref_cell.first.inner_text()).strip()
            raw = re.sub(r"[ \t]+", " ", raw)
            pairs = re.findall(r"(\d+)\s*\.\s*([A-Za-zÅÄÖåäö][^(\n]+)", raw)
            if pairs:
                kusk_pref = " | ".join(f"{n}. {nm.strip()}" for n, nm in pairs)
            else:
                a = pref_cell.first.locator("a")
                cnt = await a.count()
                if cnt > 0:
                    names = [(await a.nth(i).inner_text()).strip() for i in range(cnt)]
                    kusk_pref = " | ".join(f"{i+1}. {nm}" for i, nm in enumerate(names))

        out.append(PropRow(startdatum, bankod, namn, prop_num, dist_val, kusk_pref))

    await policy.check_page(page)
    return out


async def fetch_prop_ids_for_day(page, policy: ScrapePolicy, day_id: int) -> list[int]:
    list_url = f"https://sportapp.travsport.se/propositions/raceday/ts{day_id}"
    await policy.goto(page, list_url)
    link_sel = f"a[href*='/propositions/raceday/ts{day_id}/proposition/ts']"
    await policy.wait_for_selector(page, link_sel)

    scroller = page.locator("div.MuiDataGrid-virtualScroller, div[class*='MuiDataGrid-virtualScroller']")
    ids = set()
    previous_sample = None
    stable_bottom_samples = 0
    for _ in range(40):
        await policy.check_page(page)
        hrefs = await page.locator(link_sel).evaluate_all(
            "links => links.map(link => link.getAttribute('href')).filter(Boolean)"
        )
        for href in hrefs:
            match = re.search(r"/proposition/ts(\d+)", href)
            if match and int(match.group(1)) > 0:
                ids.add(int(match.group(1)))
        if await scroller.count() > 0:
            scroll_state = await scroller.first.evaluate("""el => ({
                top: el.scrollTop, height: el.scrollHeight, viewport: el.clientHeight
            })""")
        else:
            scroll_state = await page.evaluate("""() => {
                const el = document.scrollingElement || document.documentElement;
                return {top: el.scrollTop, height: el.scrollHeight, viewport: el.clientHeight};
            }""")
        at_bottom = (
            scroll_state["viewport"] > 0
            and scroll_state["top"] + scroll_state["viewport"] >= scroll_state["height"] - 2
        )
        sample = (len(ids), scroll_state["top"], scroll_state["height"], scroll_state["viewport"])
        loading = await page.locator(
            ".MuiDataGrid-loadingOverlay:visible, [role='progressbar']:visible, [aria-busy='true']:visible"
        ).count()
        if at_bottom and hrefs and not loading and sample == previous_sample:
            stable_bottom_samples += 1
        else:
            stable_bottom_samples = 0
        if stable_bottom_samples >= 3:
            await policy.check_page(page)
            return sorted(ids)
        previous_sample = sample
        if not at_bottom:
            if await scroller.count() > 0:
                await scroller.first.evaluate("el => el.scrollBy(0, Math.max(1, el.clientHeight * 0.8))")
            else:
                await page.evaluate("window.scrollBy(0, Math.max(1, window.innerHeight * 0.8))")
        # Give queued, paced requests time to update a virtualized list. One
        # unchanged count does not prove that the final rows have arrived.
        await asyncio.sleep(max(5, policy.request_max) + random.uniform(1, 3))

    await policy.check_page(page)
    raise ValueError("Proposition list did not settle at the bottom; discovery was not cached")


def save_rows(rows: list[PropRow]) -> None:
    close_old_connections()
    try:
        with transaction.atomic():
            for row in rows:
                Proposition.objects.update_or_create(
                    startdatum=row.startdatum,
                    bankod=row.bankod,
                    namn=row.namn,
                    proposition=row.proposition,
                    defaults={"distans": row.distans, "kuskanskemal": row.kuskanskemal},
                )
    finally:
        close_old_connections()


class Command(BaseCommand):
    help = "Scrape propositions for an explicitly selected raceday ID or short ID range."

    def add_arguments(self, parser):
        parser.add_argument("--start-id", type=int, required=True, help="First raceday ID to fetch.")
        parser.add_argument("--end-id", type=int, help="Last raceday ID (defaults to --start-id).")

    def handle(self, *args, **opts):
        start_id = opts["start_id"]
        end_id = opts["end_id"] if opts["end_id"] is not None else start_id
        if start_id <= 0 or end_id < start_id:
            raise CommandError("Use positive raceday IDs with --end-id >= --start-id.")
        with ScrapePolicy() as policy:
            grand_total = asyncio.run(self.scrape(policy, start_id, end_id))
        self.stdout.write(self.style.SUCCESS(f"Done. {grand_total} rows processed."))

    async def scrape(self, policy: ScrapePolicy, start_id: int, end_id: int) -> int:
        base_prop = "https://sportapp.travsport.se/propositions/raceday/ts{}/proposition/ts{}"
        grand_total = 0
        async with policy.browser() as page:
            for day_id in range(start_id, end_id + 1):
                policy.check()
                list_url = f"https://sportapp.travsport.se/propositions/raceday/ts{day_id}"
                cache_key = f"proposition-ids:{day_id}"
                prop_ids = policy.cached(cache_key, ttl_seconds=6 * 60 * 60)
                if prop_ids is None:
                    logging.info("Raceday ts%d: fetching proposition links", day_id)
                    try:
                        prop_ids = await fetch_prop_ids_for_day(page, policy, day_id)
                    except ScrapeStopped:
                        raise
                    except (PlaywrightError, ValueError, KeyError) as exc:
                        await policy.failure(page, list_url, str(exc))
                        continue
                    if not prop_ids:
                        await policy.failure(page, list_url, "No proposition links found")
                        continue
                    policy.remember(cache_key, prop_ids)
                    policy.success(list_url)
                else:
                    logging.info("Raceday ts%d: reusing cached proposition links", day_id)

                day_total = 0
                for pid in prop_ids:
                    policy.check()
                    url = base_prop.format(day_id, pid)
                    if policy.is_recent(url, ttl_seconds=12 * 60 * 60):
                        logging.info("Skipping recently saved proposition %s", url)
                        continue
                    logging.info("Scraping %s", url)
                    try:
                        rows = await scrape_proposition_page(page, policy, url)
                    except ScrapeStopped:
                        raise
                    except (PlaywrightError, ValueError, KeyError) as exc:
                        await policy.failure(page, url, str(exc))
                        continue
                    if not rows:
                        await policy.failure(page, url, "No proposition rows found")
                        continue
                    policy.check()
                    await asyncio.to_thread(save_rows, rows)
                    policy.success(url)
                    day_total += len(rows)
                    grand_total += len(rows)
                    logging.info("Inserted/updated %d rows", len(rows))
                logging.info("Finished raceday ts%d: %d rows", day_id, day_total)
        return grand_total
