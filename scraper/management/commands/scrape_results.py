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


# ---------------------------
# Normalisering / parsing (Java-logik portad)
# ---------------------------

def normalize_cell_text(s: str) -> str:  # //Changed!
    if s is None:  # //Changed!
        return ""  # //Changed!
    return s.replace("\u00a0", " ").strip()  # //Changed!

def normalize_name(name: str) -> str:  # //Changed!
    # Remove trailing/embedded * and normalize whitespace  # //Changed!
    cleaned = normalize_cell_text(name).replace("*", "")  # //Changed!
    return re.sub(r"\s+", " ", cleaned).strip()  # //Changed!


def normalize_kusk(kusk: str) -> str:  # //Changed!
    return re.sub(r"\s+", " ", normalize_cell_text(kusk)).strip()[:80]  # //Changed!

def sanitize_underlag(raw: str) -> str:  # //Changed!
    t = normalize_cell_text(raw).lower()  # //Changed!
    if not t:  # //Changed!
        return ""  # //Changed!
    t = t.replace("(", "").replace(")", "")  # //Changed!
    t = re.sub(r"\s+", "", t)  # //Changed!
    t = re.sub(r"[^a-z]", "", t)  # //Changed!
    return t[:2]  # //Changed!  # underlag max_length=2

# Distans/spår kan komma i olika format, vi stödjer både:
#  - "3/2140n" (vanligast här)
#  - "2140:3" (Java popup-stil)
#  - "2140" (fallback -> spår=1)
dist_slash_re = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{3,4})\s*([a-zA-Z() \u00a0]*)\s*$", re.I)  # //Changed!
dist_colon_re = re.compile(r"^\s*(\d{3,4})\s*:\s*(\d{1,2})\s*$", re.I)  # //Changed!
dist_only_re = re.compile(r"^\s*(\d{3,4})\s*(?:m)?\s*$", re.I)  # //Changed!

def parse_dist_spar(txt: str):  # //Changed!
    """
    Returns (distans, spar, underlag)
    "3/2140n" -> (2140, 3, "n")
    "2140:3"  -> (2140, 3, "")
    "2140"    -> (2140, 1, "")
    """  # //Changed!
    t = normalize_cell_text(txt)  # //Changed!
    if not t:  # //Changed!
        return None, None, ""  # //Changed!

    m = dist_slash_re.match(t)  # //Changed!
    if m:  # //Changed!
        spar = int(m.group(1))  # //Changed!
        distans = int(m.group(2))  # //Changed!
        underlag = sanitize_underlag(m.group(3))  # //Changed!
        return distans, spar, underlag  # //Changed!

    m = dist_colon_re.match(t)  # //Changed!
    if m:  # //Changed!
        distans = int(m.group(1))  # //Changed!
        spar = int(m.group(2))  # //Changed!
        return distans, spar, ""  # //Changed!

    m = dist_only_re.match(t)  # //Changed!
    if m:  # //Changed!
        distans = int(m.group(1))  # //Changed!
        return distans, 1, ""  # //Changed!  # Java-fallback: spår=1

    return None, None, ""  # //Changed!

# Placering: Java-logik
placering_with_r = re.compile(r"^(\d{1,2})r$", re.I)  # //Changed!

def map_placering_value(raw: str):  # //Changed!
    t = normalize_cell_text(raw).lower()  # //Changed!
    if not t:  # //Changed!
        return None  # //Changed!

    token = re.split(r"\s+", t, 1)[0]  # //Changed!
    token = re.sub(r"[^0-9a-zåäö]", "", token)  # //Changed!
    if not token:  # //Changed!
        return None  # //Changed!

    mr = placering_with_r.match(token)  # //Changed!
    if mr:  # //Changed!
        token = mr.group(1)  # //Changed!

    if token in ("k", "p", "str", "d"):  # //Changed!
        return 99  # //Changed!

    if not token.isdigit() or len(token) > 2:  # //Changed!
        return None  # //Changed!

    try:  # //Changed!
        v = int(token)  # //Changed!
    except ValueError:  # //Changed!
        return None  # //Changed!

    if v == 0 or v == 9:  # //Changed!
        return 15  # //Changed!

    return v  # //Changed!

# Tid: Java-logik (99.0 för dist/kub/vmk/u/d samt “heltal utan sep” när bokstäver finns)
TIME_VALUE = re.compile(r"(?:\d+\.)?(\d{1,2})[.,](\d{1,2})")  # //Changed!

def parse_tid_cell(raw: str):  # //Changed!
    t = normalize_cell_text(raw).lower()  # //Changed!
    if not t:  # //Changed!
        return None, "", ""  # //Changed!

    # ta bort parenteser + whitespace
    t2 = re.sub(r"[()\s]", "", t)  # //Changed!

    letters = re.sub(r"[0-9\.,]", "", t2)  # //Changed!
    startmetod = "a" if "a" in letters else ""  # //Changed!
    galopp = "g" if "g" in letters else ""  # //Changed!

    force99 = ("dist" in letters) or ("kub" in letters) or ("vmk" in letters) or ("u" in letters) or ("d" in letters)  # //Changed!

    tid = None  # //Changed!
    m = TIME_VALUE.search(t2)  # //Changed!
    if m:  # //Changed!
        try:  # //Changed!
            tid = float(f"{m.group(1)}.{m.group(2)}")  # //Changed!
        except ValueError:  # //Changed!
            tid = None  # //Changed!

    if force99:  # //Changed!
        return 99.0, startmetod, galopp  # //Changed!

    # Om ingen komma/punkt finns men det finns siffror (<=2) och bokstäver => tid=99
    if tid is None:  # //Changed!
        has_sep = ("," in t2) or ("." in t2)  # //Changed!
        digits = re.sub(r"\D+", "", t2)  # //Changed!
        if (not has_sep) and digits and len(digits) <= 2 and letters:  # //Changed!
            return 99.0, startmetod, galopp  # //Changed!

    return tid, startmetod, galopp  # //Changed!


# ---------------------------
# Bankod mapping (oförändrat)
# ---------------------------

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
    name_up = normalize_cell_text(name).upper()
    if name_up in FULLNAME_TO_BANKOD:
        return FULLNAME_TO_BANKOD[name_up]
    name_ascii = _strip_diacritics(name_up)
    return _ASCII_FALLBACK.get(name_ascii, name_up[:2].title())


# ---------------------------
# Dataclass
# ---------------------------

@dataclass
class Row:
    datum: int
    bankod: str
    lopp: int
    nr: int
    namn: str
    distans: int | None
    spar: int | None
    placering: int | None
    tid: float | None
    startmetod: str
    galopp: str
    underlag: str
    kusk: str  # //Changed!


# ---------------------------
# Scraper
# ---------------------------

async def scrape_page(url: str) -> List[Row]:
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

        track_raw = normalize_cell_text(await nav.nth(0).inner_text())
        bankod = track_to_bankod(track_raw)

        date_txt = normalize_cell_text(await nav.nth(1).inner_text())
        datum = int(swedish_date_to_yyyymmdd(date_txt))

        data: List[Row] = []
        lopp_headers = page.locator("//h2[starts-with(normalize-space(),'Lopp')]")
        for i in range(await lopp_headers.count()):
            header = lopp_headers.nth(i)
            m = re.search(r"Lopp\s+(\d+)", normalize_cell_text(await header.inner_text()))
            if not m:
                continue
            lopp = int(m.group(1))

            section = header.locator("xpath=ancestor::div[contains(@class,'MuiBox-root')][1]")
            rows = await section.locator("div[role='row'][data-rowindex]").all()
            if not rows:
                continue

            for row in rows:
                cell = lambda f: row.locator(f"div[data-field='{f}']")

                nr_txt = normalize_cell_text(await cell("horse").locator("div").first.inner_text())
                nr_m = re.search(r"\d+", nr_txt)  # //Changed!
                if not nr_m:  # //Changed!
                    continue  # //Changed!
                nr = int(nr_m.group(0))  # //Changed!

                namn_raw = normalize_cell_text(await cell("horse").locator("span").first.inner_text())
                namn = normalize_name(namn_raw.split("(")[0])

                # Kusk (driver)
                kusk = ""  # //Changed!
                try:  # //Changed!
                    drv = cell("driver")  # //Changed!
                    a = drv.locator("a")  # //Changed!
                    kusk_raw = (await a.first.inner_text()).strip() if await a.count() > 0 else normalize_cell_text(await drv.inner_text())  # //Changed!
                    kusk = normalize_kusk(kusk_raw)  # //Changed!
                except Exception:  # //Changed!
                    kusk = ""  # //Changed!

                # Placering (Java-logik)
                placetxt = normalize_cell_text(await cell("placementDisplay").inner_text())
                placering = map_placering_value(placetxt)  # //Changed!

                # Distans/spår/underlag (robust + sanitize)
                dist_raw = normalize_cell_text(await cell("startPositionAndDistance").inner_text())
                distans, spar, underlag = parse_dist_spar(dist_raw)  # //Changed!

                # Tid/startmetod/galopp (Java-logik)
                tid_raw = normalize_cell_text(await cell("time").inner_text())
                tid, startmetod, galopp = parse_tid_cell(tid_raw)  # //Changed!

                data.append(Row(
                    datum=datum,
                    bankod=bankod,
                    lopp=lopp,
                    nr=nr,
                    namn=namn,
                    distans=distans,
                    spar=spar,
                    placering=placering,
                    tid=tid,
                    startmetod=startmetod,
                    galopp=galopp,
                    underlag=underlag,
                    kusk=kusk,  # //Changed!
                ))

        await browser.close()
        return data


# ---------------------------
# Django management command
# ---------------------------

class Command(BaseCommand):
    help = "Scrape hard-coded ts-ID range into Result"

    START_ID = 610_355
    END_ID = 610_420

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
                namn_clean = normalize_name(r.namn)

                HorseResult.objects.update_or_create(
                    datum=r.datum,
                    bankod=r.bankod,
                    lopp=r.lopp,
                    namn=namn_clean,
                    defaults=dict(
                        nr=r.nr,
                        distans=r.distans,
                        spar=r.spar,
                        placering=r.placering,
                        tid=r.tid,
                        startmetod=r.startmetod,
                        galopp=r.galopp,
                        underlag=r.underlag,
                        kusk=r.kusk,  # //Changed!
                    ),
                )

            total += len(rows)
            logging.info("  inserted/updated %d rows", len(rows))

        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows processed."))
