# scraper/management/commands/scrape_results.py
import asyncio, re, unicodedata, logging
from dataclasses import dataclass
from typing import List
from playwright.async_api import async_playwright, Error as PlaywrightError
from django.core.management.base import BaseCommand
from scraper.models import HorseResult

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}

def swedish_date_to_yyyymmdd(text: str) -> str:
    parts = text.strip().upper().split()
    if len(parts) == 4:        
        _, d, m, y = parts
    else:                    
        d, m, y = parts
    return f"{int(y):04d}{SWEDISH_MONTH[m]:02d}{int(d):02d}"


dist_re = re.compile(r"\s*(\d+)\s*/\s*(\d+)\s*([ntv]?)\s*$", re.I)
time_re = re.compile(r"\s*([\d.,]+)\s*([ag]*)", re.I)

def parse_dist_spar(txt: str):
    """
    Returns (distans, spar, underlag_letter)
    3/2140n → (2140, 3, 'n')     6/1640t → (1640, 6, 't')     1/2640 → (2640, 1, '')
    """
    m = dist_re.match(txt)
    if not m:
        return None, None, ""
    spar, distans, underlag = int(m[1]), int(m[2]), m[3].lower()
    return distans, spar, underlag

def parse_tid_block(txt: str):
    m = time_re.match(txt)
    if not m:
        return None, "", ""
    tid = float(m[1].replace(",", "."))
    flags = m[2].lower()
    return tid, ("a" if "a" in flags else ""), ("g" if "g" in flags else "")


FULLNAME_TO_BANKOD = {
    "ARVIKA": "Ar",  "AXEVALLA": "Ax",  "BERGSÅKER": "B",  "BODEN": "Bo",
    "BOLLNÄS": "Bs", "DANNERO": "D",   "DALA JÄRNA": "Dj","ESKILSTUNA": "E",
    "JÄGERSRO": "J", "FÄRJESTAD": "F", "GÄVLE": "G",      "GÖTEBORG TRAV": "Gt",
    "HAGMYREN": "H", "HALMSTAD": "Hd", "HOTING": "Hg",    "KARLSHAMN": "Kh",
    "KALMAR": "Kr",  "LINDESBERG": "L","LYCKSELE": "Ly",  "MANTORP": "Mp",
    "OVIKEN": "Ov",  "ROMME": "Ro",    "RÄTTVIK": "Rä",   "SOLVALLA": "S",
    "SKELLEFTEÅ": "Sk","SOLÄNGET": "Sä","TINGSRYD": "Ti", "TÄBY TRAV": "Tt",
    "UMÅKER": "U",   "VEMDALEN": "Vd", "VAGGERYD": "Vg",  "VISBY": "Vi",
    "ÅBY": "Å",      "ÅMÅL": "Åm",     "ÅRJÄNG": "År",    "ÖREBRO": "Ö",
    "ÖSTERSUND": "Ös",
}

def _strip_diacritics(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

_ASCII_FALLBACK = { _strip_diacritics(k): v for k, v in FULLNAME_TO_BANKOD.items() }

def track_to_bankod(name: str) -> str:
    name_up = name.strip().upper()
    if name_up in FULLNAME_TO_BANKOD:
        return FULLNAME_TO_BANKOD[name_up]
    name_ascii = _strip_diacritics(name_up)
    return _ASCII_FALLBACK.get(name_ascii, name_up[:2].title())


@dataclass
class Row:
    datum: int; bankod: str; lopp: int; nr: int; namn: str
    distans: int | None; spar: int | None; placering: int | None
    tid: float | None; startmetod: str; galopp: str; underlag: str


async def scrape_page(url: str) -> List[Row]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(); ctx.set_default_timeout(120_000)
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=0)
        except PlaywrightError:
            await browser.close(); return []

        try:  
            await page.wait_for_selector("div[role='row'][data-rowindex]", timeout=10_000)  
        except PlaywrightError:  
            await browser.close(); return []  

        nav = page.locator("div[class*='RaceDayNavigator_title'] span")
        if await nav.count() < 2:
            await browser.close(); return []

        track_raw = (await nav.nth(0).inner_text()).strip()
        bankod    = track_to_bankod(track_raw)
        date_txt  = (await nav.nth(1).inner_text()).strip()
        datum     = int(swedish_date_to_yyyymmdd(date_txt))

        data: List[Row] = []
        lopp_headers = page.locator("//h2[starts-with(normalize-space(),'Lopp')]")
        for i in range(await lopp_headers.count()):
            header = lopp_headers.nth(i)
            m = re.search(r"Lopp\s+(\d+)", (await header.inner_text()).strip())
            if not m:
                continue
            lopp = int(m[1])

            section = header.locator(
                "xpath=ancestor::div[contains(@class,'MuiBox-root')][1]"
            )
            rows = await section.locator("div[role='row'][data-rowindex]").all()
            if not rows:
                continue

            for row in rows:
                cell = lambda f: row.locator(f"div[data-field='{f}']")
                nr = int(re.match(
                    r"\d+",
                    (await cell("horse").locator("div").first.inner_text()).strip()
                )[0])
                namn = (await cell("horse").locator("span").first.inner_text())\
                       .split("(")[0].strip()

                placetxt = (await cell("placementDisplay").inner_text()).strip()
                placering = int(placetxt) if placetxt.isdigit() else None

                dist_raw = (await cell("startPositionAndDistance").inner_text()).strip()
                distans, spar, underlag = parse_dist_spar(dist_raw)

                tid_raw = (await cell("time").inner_text()).strip()
                tid, startmetod, galopp = parse_tid_block(tid_raw)

                data.append(Row(
                    datum, bankod, lopp, nr, namn,
                    distans, spar, placering, tid,
                    startmetod, galopp, underlag
                ))

        await browser.close()
        return data

class Command(BaseCommand):
    help = "Scrape fixed ID range 609766 → 609963 into Resultat"

    START_ID = 609_974
    END_ID   = 609_986

    def handle(self, *args, **opts):
        base = "https://sportapp.travsport.se/race/raceday/ts{}/results/all"
        total = 0

        for ts_id in range(self.START_ID, self.END_ID + 1):
            url = base.format(ts_id)
            logging.info("Scraping %s", url)

            try:
                rows = asyncio.run(scrape_page(url))
            except Exception as exc:
                logging.warning("  failed: %s", exc)
                continue

            if not rows:
                logging.info("  no rows")
                continue

            for r in rows:
                HorseResult.objects.update_or_create(
                    datum=r.datum, bankod=r.bankod, lopp=r.lopp, nr=r.nr,
                    defaults=dict(
                        namn       = r.namn,
                        distans    = r.distans,
                        spar       = r.spar,
                        placering  = r.placering,
                        tid        = r.tid,
                        startmetod = r.startmetod,
                        galopp     = r.galopp,
                        underlag   = r.underlag,
                    ),
                )
            total += len(rows)
            logging.info("  inserted/updated %d rows", len(rows))

        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows processed."))
