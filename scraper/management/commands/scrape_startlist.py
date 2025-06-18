# scraper/management/commands/scrape_startlist.py
import asyncio, re, unicodedata, logging
from dataclasses import dataclass
from typing import List
from playwright.async_api import async_playwright, Error as PlaywrightError
from django.core.management.base import BaseCommand
from scraper.models import StartList

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ───────── helpers ─────────
SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}
def swedish_date_to_yyyymmdd(txt: str) -> str:
    p = txt.strip().upper().split()
    d, m, y = (p[1], p[2], p[3]) if len(p) == 4 else p
    return f"{int(y):04d}{SWEDISH_MONTH[m]:02d}{int(d):02d}"

dist_re = re.compile(r"\s*(\d+)\s*/\s*([\d,]+)", re.I)   # allow 1,609
def parse_dist_spar(txt: str):
    m = dist_re.match(txt)
    if not m:
        return None, None
    spar = int(m[1])
    dist = int(m[2].replace(",", ""))   # "1,609" → 1609
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
    "ÖREBRO":"Ö","ÖSTERSUND":"Ös",
}
FULLNAME_TO_BANKOD |= {_strip(k): v for k, v in FULLNAME_TO_BANKOD.items()}
def track_to_bankod(n: str) -> str:
    n = n.strip().upper()
    return FULLNAME_TO_BANKOD.get(n, FULLNAME_TO_BANKOD.get(_strip(n), n[:2].title()))

# ───────── DTO ─────────
@dataclass
class StartRow:
    startdatum: int; bankod: str; lopp: int; nr: int; namn: str
    spar: int | None; distans: int | None; kusk: str

# ───────── scrape one page ─────────
async def scrape_startlist(url: str) -> List[StartRow]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(); ctx.set_default_timeout(120_000)
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=0)
        except PlaywrightError:
            await browser.close(); return []

        # wait until grid rows exist (react finished)
        try:
            await page.wait_for_selector("div[role='row'][data-rowindex]", timeout=10_000)
        except PlaywrightError:
            await browser.close(); return []

        nav = page.locator("div[class*='RaceDayNavigator_title'] span")
        if await nav.count() < 2:
            await browser.close(); return []

                # first span can be "TÄVLINGSDAG TINGSRYD", "TRAVTÄVLING SOLVALLA", etc.
        raw_track  = (await nav.nth(0).inner_text()).strip().upper()
        if raw_track.startswith(("TÄVLINGSDAG", "TRAVTÄVLING")):
            raw_track = raw_track.split(maxsplit=1)[1]          # keep second word
        bankod     = track_to_bankod(raw_track)

        
        startdatum = int(swedish_date_to_yyyymmdd((await nav.nth(1).inner_text()).strip()))

        out: List[StartRow] = []
        lopp_headers = page.locator("//h2[starts-with(normalize-space(),'Lopp')]")
        for i in range(await lopp_headers.count()):
            header = lopp_headers.nth(i)
            m = re.search(r"Lopp\s+(\d+)", (await header.inner_text()).strip())
            if not m:
                continue
            lopp_nr = int(m[1])

            section = header.locator("xpath=ancestor::div[contains(@class,'MuiBox-root')][1]")
            await section.locator("div[role='row'][data-rowindex]").first.wait_for()
            rows = await section.locator("div[role='row'][data-rowindex]").all()

            for row in rows:
                cell = lambda f: row.locator(f"div[data-field='{f}']")
                nr = int(re.match(r"\d+", (await cell("horse").locator("div").first.inner_text()).strip())[0])
                namn = (await cell("horse").locator("span").first.inner_text()).split("(")[0].strip()
                kusk = (await cell("driver").inner_text()).strip()
                dist_raw = (await cell("trackName").inner_text()).strip()
                distans, spar = parse_dist_spar(dist_raw)
                out.append(StartRow(startdatum, bankod, lopp_nr, nr, namn, spar, distans, kusk))

        await browser.close()
        return out

# ───────── management command ─────────
class Command(BaseCommand):
    START_ID = 609_937
    END_ID   = 609_967
    help = "Scrape hard-coded ts-ID range into Startlista"

    def handle(self, *args, **kwargs):
        base = "https://sportapp.travsport.se/race/raceday/ts{}/startlist/all"
        total = 0
        for ts in range(self.START_ID, self.END_ID + 1):
            url = base.format(ts)
            logging.info("Scraping %s", url)
            try:
                rows = asyncio.run(scrape_startlist(url))
            except Exception as exc:
                logging.warning("  failed: %s", exc); continue

            if not rows:
                logging.info("  no rows"); continue

            for r in rows:
                StartList.objects.update_or_create(
                    startdatum=r.startdatum, bankod=r.bankod,
                    lopp=r.lopp, nr=r.nr,
                    defaults=dict(namn=r.namn, spar=r.spar,
                                  distans=r.distans, kusk=r.kusk)
                )
            total += len(rows)
            logging.info("  inserted/updated %d rows", len(rows))

        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows processed."))
