import asyncio, re, unicodedata, logging
from dataclasses import dataclass
from typing import List, Optional  # //Changed!
from datetime import date  # //Changed!
from django.utils import timezone  # //Changed!
from playwright.async_api import async_playwright, Error as PlaywrightError
from django.core.management.base import BaseCommand
from scraper.models import StartList, HorseResult  # //Changed!

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


# ---------------------------
# Normalisering
# ---------------------------

def normalize_cell_text(s: str) -> str:  # //Changed!
    if s is None:  # //Changed!
        return ""  # //Changed!
    return s.replace("\u00a0", " ").strip()  # //Changed!

def trim_to_max(s: str, max_len: int) -> str:  # //Changed!
    s = s or ""  # //Changed!
    return s if len(s) <= max_len else s[:max_len]  # //Changed!

_paren_re = re.compile(r"\([^)]*\)")  # //Changed!

def normalize_startlista_name(name: str) -> str:  # //Changed!
    cleaned = normalize_cell_text(name)  # //Changed!

    cleaned = cleaned.replace("*", "")  # //Changed!
    cleaned = cleaned.replace("'", "").replace("’", "")  # //Changed! (både ' och ’)
    cleaned = _paren_re.sub("", cleaned)  # //Changed! (ta bort parentes + innehåll)

    if len(cleaned) >= 7:  # //Changed!
        cleaned = cleaned[:-7]  # //Changed! (ta bort sista 7 tecken)

    cleaned = cleaned.rstrip()  # //Changed! (trim i slutet)
    cleaned = cleaned.upper()  # //Changed! (UPPERCASE)

    return trim_to_max(cleaned, 50)  # //Changed!

def normalize_kusk(kusk: str, max_len: int) -> str:  # //Changed!
    cleaned = re.sub(r"\s+", " ", normalize_cell_text(kusk)).strip()  # //Changed!
    return trim_to_max(cleaned, max_len)  # //Changed!


# ---------------------------
# Distans/spår (startlista)
# ---------------------------

dist_re = re.compile(r"\s*(\d+)\s*/\s*([\d,]+)", re.I)

def parse_dist_spar(txt: str):
    t = normalize_cell_text(txt)  # //Changed!
    m = dist_re.match(t)
    if not m:
        return None, None
    spar = int(m.group(1))
    dist = int(m.group(2).replace(",", ""))
    return dist, spar


# ---------------------------
# Bankod mapping (oförändrat)
# ---------------------------

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
    n = normalize_cell_text(n).upper()  # //Changed!
    return FULLNAME_TO_BANKOD.get(n, FULLNAME_TO_BANKOD.get(_strip(n), n[:2].title()))


# ---------------------------
# Dataclass
# ---------------------------

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


# ---------------------------
# Scraper
# ---------------------------

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

        raw_track = normalize_cell_text(await nav.nth(0).inner_text()).upper()  # //Changed!
        if raw_track.startswith(("TÄVLINGSDAG", "TRAVTÄVLING")):
            parts = raw_track.split(maxsplit=1)
            raw_track = parts[1] if len(parts) > 1 else raw_track

        bankod = track_to_bankod(raw_track)

        startdatum = int(swedish_date_to_yyyymmdd(normalize_cell_text(await nav.nth(1).inner_text())))  # //Changed!

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

                nr_txt = normalize_cell_text(await cell("horse").locator("div").first.inner_text())  # //Changed!
                nr_m = re.search(r"\d+", nr_txt)  # //Changed!
                if not nr_m:  # //Changed!
                    continue  # //Changed!
                nr = int(nr_m.group(0))  # //Changed!

                namn_raw = normalize_cell_text(await cell("horse").locator("span").first.inner_text())  # //Changed!
                namn = normalize_startlista_name(namn_raw)  # //Changed!

                kusk_raw = normalize_cell_text(await cell("driver").inner_text())  # //Changed!
                kusk = normalize_kusk(kusk_raw, 120)  # //Changed! (startlista max 120)

                dist_raw = normalize_cell_text(await cell("trackName").inner_text())  # //Changed!
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


# ---------------------------
# DB write helpers
# ---------------------------

def _today_yyyymmdd() -> int:  # //Changed!
    d: date = timezone.localdate()  # //Changed!
    return d.year * 10000 + d.month * 100 + d.day  # //Changed!

def upsert_resultat_from_startrow(r: StartRow):  # //Changed!
    namn_clean = r.namn  # //Changed! (viktigt: normalisera INTE igen, annars tas sista 7 tecken bort två gånger)
    kusk_res = normalize_kusk(r.kusk, 80)  # //Changed! (resultat max 80)

    obj, created = HorseResult.objects.get_or_create(  # //Changed!
        datum=r.startdatum,  # //Changed!
        bankod=r.bankod,  # //Changed!
        lopp=r.lopp,  # //Changed!
        namn=namn_clean,  # //Changed!
        defaults=dict(  # //Changed!
            nr=r.nr,  # //Changed!
            distans=r.distans,  # //Changed!
            spar=r.spar,  # //Changed!
            kusk=kusk_res,  # //Changed!
            placering=0,
            # //Changed! odds lämnas helt orörd här (vi sätter den aldrig från startlista)
        ),  # //Changed!
    )  # //Changed!

    if created:  # //Changed!
        return  # //Changed!

    changed_fields = []  # //Changed!

    if obj.nr != r.nr:  # //Changed!
        obj.nr = r.nr  # //Changed!
        changed_fields.append("nr")  # //Changed!

    if r.distans is not None and obj.distans != r.distans:  # //Changed!
        obj.distans = r.distans  # //Changed!
        changed_fields.append("distans")  # //Changed!

    if r.spar is not None and obj.spar != r.spar:  # //Changed!
        obj.spar = r.spar  # //Changed!
        changed_fields.append("spar")  # //Changed!

    if kusk_res and obj.kusk != kusk_res:  # //Changed!
        obj.kusk = kusk_res  # //Changed!
        changed_fields.append("kusk")  # //Changed!
        
    if obj.placering is None:  # //Changed!
        obj.placering = 0  # //Changed!
        changed_fields.append("placering")  # //Changed!

    # //Changed! OBS: odds uppdateras aldrig här, så en befintlig odds (t.ex. 30) kan inte skrivas över.
    if changed_fields:  # //Changed!
        obj.save(update_fields=changed_fields)  # //Changed!


# ---------------------------
# Django management command
# ---------------------------

class Command(BaseCommand):
    START_ID = 610_385
    END_ID   = 610_450
    help = "Scrape hard-coded ts-ID range into Startlista (and also seed Resultat for today/future only)"  # //Changed!

    def handle(self, *args, **kwargs):
        base = "https://sportapp.travsport.se/race/raceday/ts{}/startlist/all"
        total = 0
        total_resultat = 0  # //Changed!
        today_int = _today_yyyymmdd()  # //Changed!

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
                        namn=r.namn,  # //Changed! (inte normalisera igen)
                        spar=r.spar,
                        distans=r.distans,
                        kusk=normalize_kusk(r.kusk, 120),  # //Changed!
                    ),
                )

                if r.startdatum >= today_int:  # //Changed!
                    upsert_resultat_from_startrow(r)  # //Changed!
                    total_resultat += 1  # //Changed!

            total += len(rows)
            logging.info(
                "  inserted/updated %d startlista rows (+%d resultat upserts, today=%d)",
                len(rows), total_resultat, today_int  # //Changed!
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. {total} startlista rows processed. {total_resultat} resultat upserts (today/future only)."
        ))  # //Changed!
