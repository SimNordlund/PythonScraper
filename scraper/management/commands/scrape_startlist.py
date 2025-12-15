import asyncio, re, unicodedata, logging
from dataclasses import dataclass
from typing import List, Optional  
from datetime import date  
from django.utils import timezone  
from playwright.async_api import async_playwright, Error as PlaywrightError
from django.core.management.base import BaseCommand
from scraper.models import StartList, HorseResult  

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}

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
    n = normalize_cell_text(n).upper()  
    return FULLNAME_TO_BANKOD.get(n, FULLNAME_TO_BANKOD.get(_strip(n), n[:2].title()))


@dataclass
class StartRow:
    startdatum: int
    bankod: str
    lopp: int
    nr: int
    namn: str
    spar: Optional[int]
    distans: Optional[int]
    kusk: str


async def scrape_startlist(url: str) -> List[StartRow]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        ctx.set_default_timeout(120_000)
        page = await ctx.new_page()

        try:
            await page.goto(url, timeout=0)
        except PlaywrightError:
            await browser.close()
            return []

        try:
            await page.wait_for_selector("div[role='row'][data-rowindex]", timeout=10_000)
        except PlaywrightError:
            await browser.close()
            return []

        nav = page.locator("div[class*='RaceDayNavigator_title'] span")
        if await nav.count() < 2:
            await browser.close()
            return []

        raw_track = normalize_cell_text(await nav.nth(0).inner_text()).upper()  
        if raw_track.startswith(("TÄVLINGSDAG", "TRAVTÄVLING")):
            parts = raw_track.split(maxsplit=1)
            raw_track = parts[1] if len(parts) > 1 else raw_track

        bankod = track_to_bankod(raw_track)

        startdatum = int(swedish_date_to_yyyymmdd(normalize_cell_text(await nav.nth(1).inner_text())))  

        out: List[StartRow] = []
        lopp_headers = page.locator("//h2[starts-with(normalize-space(),'Lopp')]")
        for i in range(await lopp_headers.count()):
            header = lopp_headers.nth(i)
            m = re.search(r"Lopp\s+(\d+)", normalize_cell_text(await header.inner_text()))
            if not m:
                continue
            lopp_nr = int(m.group(1))

            section = header.locator("xpath=ancestor::div[contains(@class,'MuiBox-root')][1]")
            rows = await section.locator("div[role='row'][data-rowindex]").all()
            if not rows:
                logging.info("Lopp %s: inga rader, hoppar över", lopp_nr)
                continue

            for row in rows:
                cell = lambda f: row.locator(f"div[data-field='{f}']")

                nr_txt = normalize_cell_text(await cell("horse").locator("div").first.inner_text())  
                nr_m = re.search(r"\d+", nr_txt)  
                if not nr_m:  
                    continue  
                nr = int(nr_m.group(0))  

                namn_raw = normalize_cell_text(await cell("horse").locator("span").first.inner_text())  
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
                ))

        await browser.close()
        return out

def _today_yyyymmdd() -> int:  
    d: date = timezone.localdate()  
    return d.year * 10000 + d.month * 100 + d.day  

def upsert_resultat_from_startrow(r: StartRow):  
    namn_clean = r.namn  
    kusk_res = normalize_kusk(r.kusk, 80)  

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
            placering=0,
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
        
    if obj.placering is None:  
        obj.placering = 0  
        changed_fields.append("placering")  

    if changed_fields:  
        obj.save(update_fields=changed_fields)  



class Command(BaseCommand):
    START_ID = 610_390
    END_ID   = 610_450
    
    # Slutade ts609932
    
    # 1 jan 2025 609601
    
    help = "Scrape hard-coded ts-ID range into Startlista (and also seed Resultat for today/future only)"  

    def handle(self, *args, **kwargs):
        base = "https://sportapp.travsport.se/race/raceday/ts{}/startlist/all"
        total = 0
        total_resultat = 0  
        today_int = _today_yyyymmdd()  

        for ts in range(self.START_ID, self.END_ID + 1):
            url = base.format(ts)
            logging.info("Scraping %s", url)

            try:
                rows = asyncio.run(scrape_startlist(url))
            except Exception as exc:
                logging.warning("  failed: %s", exc)
                continue

            if not rows:
                logging.info("  no rows")
                continue

            for r in rows:
                StartList.objects.update_or_create(
                    startdatum=r.startdatum,
                    bankod=r.bankod,
                    lopp=r.lopp,
                    nr=r.nr,
                    defaults=dict(
                        namn=r.namn, 
                        spar=r.spar,
                        distans=r.distans,
                        kusk=normalize_kusk(r.kusk, 120),  
                    ),
                )

                if r.startdatum >= today_int:  
                    upsert_resultat_from_startrow(r)  
                    total_resultat += 1  

            total += len(rows)
            logging.info(
                "  inserted/updated %d startlista rows (+%d resultat upserts, today=%d)",
                len(rows), total_resultat, today_int  
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. {total} startlista rows processed. {total_resultat} resultat upserts (today/future only)."
        ))  
