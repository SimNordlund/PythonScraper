# //Changed! scrape_proposition.py (ren och fixad)
import asyncio, re, unicodedata, logging  # //Changed!
from dataclasses import dataclass         # //Changed!
from typing import List                   # //Changed!
from playwright.async_api import async_playwright, Error as PlaywrightError  # //Changed!
from django.core.management.base import BaseCommand  # //Changed!
from scraper.models import Proposition  # //Changed!

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")  # //Changed!

SWEDISH_MONTH = {  # //Changed!
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}

def swedish_date_to_yyyymmdd(text: str) -> str:  # //Changed!
    p = text.strip().upper().split()
    if len(p) == 4:
        _, d, m, y = p
    else:
        d, m, y = p
    return f"{int(y):04d}{SWEDISH_MONTH[m]:02d}{int(d):02d}"

def _strip_diacritics(s: str) -> str:  # //Changed!
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

FULLNAME_TO_BANKOD = {  # //Changed!
    "ARVIKA":"Ar","AXEVALLA":"Ax","BERGSÅKER":"B","BODEN":"Bo","BOLLNÄS":"Bs",
    "DANNERO":"D","DALA JÄRNA":"Dj","ESKILSTUNA":"E","JÄGERSRO":"J","FÄRJESTAD":"F",
    "GÄVLE":"G","GÖTEBORG TRAV":"Gt","HAGMYREN":"H","HALMSTAD":"Hd","HOTING":"Hg",
    "KARLSHAMN":"Kh","KALMAR":"Kr","LINDESBERG":"L","LYCKSELE":"Ly","MANTORP":"Mp",
    "OVIKEN":"Ov","ROMME":"Ro","RÄTTVIK":"Rä","SOLVALLA":"S","SKELLEFTEÅ":"Sk",
    "SOLÄNGET":"Sä","TINGSRYD":"Ti","TÄBY TRAV":"Tt","UMÅKER":"U","VEMDALEN":"Vd",
    "VAGGERYD":"Vg","VISBY":"Vi","ÅBY":"Å","ÅMÅL":"Åm","ÅRJÄNG":"År","ÖREBRO":"Ö","ÖSTERSUND":"Ös",
}
_ASCII_FALLBACK = {_strip_diacritics(k): v for k, v in FULLNAME_TO_BANKOD.items()}  # //Changed!

def track_to_bankod(name: str) -> str:  # //Changed!
    """Direkt mappning namn -> bankod; har fallback till två första tecken om okänt."""
    name_up = name.strip().upper()
    if name_up.startswith(("TÄVLINGSDAG", "TRAVTÄVLING")):
        name_up = name_up.split(maxsplit=1)[1]
    if name_up in FULLNAME_TO_BANKOD:
        return FULLNAME_TO_BANKOD[name_up]
    return _ASCII_FALLBACK.get(_strip_diacritics(name_up), name_up[:2].title())

# //Changed! Robust extraktion: rensa symboler, hitta känd bana som delsträng
def extract_bankod_from_text(raw: str) -> str | None:  # //Changed!
    t_up = re.sub(r"[^A-ZÅÄÖ\s]", " ", raw.upper())    # rensa ikoner/specialtecken
    t_up = re.sub(r"\s+", " ", t_up).strip()
    for key in sorted(FULLNAME_TO_BANKOD.keys(), key=len, reverse=True):
        if key in t_up:
            return FULLNAME_TO_BANKOD[key]
    t_ascii = _strip_diacritics(t_up)
    for key in sorted(_ASCII_FALLBACK.keys(), key=len, reverse=True):
        if key in t_ascii:
            return _ASCII_FALLBACK[key]
    return None

@dataclass  # //Changed!
class PropRow:  # //Changed!
    startdatum: int
    bankod: str
    namn: str
    proposition: int

async def scrape_proposition_page(url: str) -> List[PropRow]:  # //Changed!
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(); ctx.set_default_timeout(120_000)
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=0)
        except PlaywrightError:
            await browser.close(); return []

        # Vänta in gridraderna (hästtabellen)  # //Changed!
        try:
            await page.wait_for_selector("div[role='row'][data-rowindex]", timeout=10_000)
        except PlaywrightError:
            await browser.close(); return []

        # 1) Bana + datum  # //Changed!
        bankod = None; startdatum = None

        # Försök A: RaceDayNavigator (om den finns på denna sida)  # //Changed!
        nav = page.locator("div[class*='RaceDayNavigator_title'] span")
        if await nav.count() >= 2:
            track_text = (await nav.nth(0).inner_text()).strip()
            bank_try = extract_bankod_from_text(track_text) or track_to_bankod(track_text)  # //Changed!
            date_text = (await nav.nth(1).inner_text()).strip()
            bankod = bank_try
            startdatum = int(swedish_date_to_yyyymmdd(date_text))

        # Försök B: “Färjestad 2025-09-01” i valfritt element  # //Changed!
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
                bank_try = extract_bankod_from_text(track_part)  # //Changed!
                bankod = bank_try or track_to_bankod(track_part)  # //Changed!
                logging.info("  bana-text: '%s' -> bankod=%s", track_part, bankod)  # //Changed!
                break

        if bankod is None or startdatum is None:  # //Changed!
            logging.info("  kunde inte hitta bana/datum")
            await browser.close(); return []
        else:
            logging.info("  hittade bana=%s datum=%s", bankod, startdatum)

        # 2) Propositionnummer (“Prop. X”) – var som helst i sidan  # //Changed!
        prop_num = None
        cand = page.locator("xpath=//*[contains(normalize-space(.), 'Prop.')]")
        for i in range(min(await cand.count(), 50)):
            txt = (await cand.nth(i).inner_text()).strip()
            m = re.search(r"Prop\.\s*(\d+)", txt, flags=re.I)
            if m:
                prop_num = int(m.group(1))
                break
        if prop_num is None:
            logging.info("  kunde inte hitta 'Prop.'-numret")
            await browser.close(); return []
        else:
            logging.info("  hittade Prop. %s", prop_num)

        # 3) Hästnamn i grid  # //Changed!
        rows = await page.locator("div[role='row'][data-rowindex]").all()
        out: List[PropRow] = []
        for row in rows:
            cell = row.locator("div[data-field='horseName'], div[data-field='horse']")
            if await cell.count() == 0:
                continue
            name_loc = cell.locator("a, span").first
            namn_raw = (await name_loc.inner_text()).strip()
            namn = namn_raw.split("(")[0].strip()
            if not namn:
                continue
            out.append(PropRow(startdatum, bankod, namn, prop_num))

        await browser.close()
        return out

class Command(BaseCommand):  # //Changed!
    help = "Scrape proposition-sidor till tabellen 'proposition'."  # //Changed!

    RACE_DAY_IDS = [610_129]  # //Changed!
    PROP_START_ID = 720_415   # //Changed!
    PROP_END_ID   = 721_900   # //Changed!

    def handle(self, *args, **opts):  # //Changed!
        base = "https://sportapp.travsport.se/propositions/raceday/ts{}/proposition/ts{}"
        total = 0
        for day_id in self.RACE_DAY_IDS:
            for prop_id in range(self.PROP_START_ID, self.PROP_END_ID + 1):
                url = base.format(day_id, prop_id)
                logging.info("Scraping %s", url)
                try:
                    rows = asyncio.run(scrape_proposition_page(url))
                except Exception as exc:
                    logging.warning("  failed: %s", exc)
                    rows = []

                if not rows:
                    logging.info("  no rows")
                    continue

                for r in rows:
                    Proposition.objects.update_or_create(
                        startdatum=r.startdatum, bankod=r.bankod,
                        namn=r.namn, proposition=r.proposition,
                        defaults={},
                    )
                total += len(rows)
                logging.info("  inserted/updated %d rows", len(rows))

        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows processed."))  # //Changed!
