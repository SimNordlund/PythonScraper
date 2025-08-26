import asyncio, re, unicodedata, logging  
from dataclasses import dataclass         
from typing import List                   
from playwright.async_api import async_playwright, Error as PlaywrightError  
from django.core.management.base import BaseCommand  
from scraper.models import Proposition  

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
    """Direkt mappning namn -> bankod; har fallback till två första tecken om okänt."""
    name_up = name.strip().upper()
    if name_up.startswith(("TÄVLINGSDAG", "TRAVTÄVLING")):
        name_up = name_up.split(maxsplit=1)[1]
    if name_up in FULLNAME_TO_BANKOD:
        return FULLNAME_TO_BANKOD[name_up]
    return _ASCII_FALLBACK.get(_strip_diacritics(name_up), name_up[:2].title())

def extract_bankod_from_text(raw: str) -> str | None:  
    """Rensa ikoner och hitta känd bana som delsträng."""
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

async def scrape_proposition_page(url: str) -> List[PropRow]:  
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(); ctx.set_default_timeout(120_000)
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=0)
        except PlaywrightError:
            await browser.close(); return []

        # Vänta in gridraderna (hästtabellen)
        try:
            await page.wait_for_selector("div[role='row'][data-rowindex]", timeout=10_000)
        except PlaywrightError:
            await browser.close(); return []

        # 1) Bana + datum
        bankod = None; startdatum = None

        # A: RaceDayNavigator (om den finns)
        nav = page.locator("div[class*='RaceDayNavigator_title'] span")
        if await nav.count() >= 2:
            track_text = (await nav.nth(0).inner_text()).strip()
            bank_try = extract_bankod_from_text(track_text) or track_to_bankod(track_text)
            date_text = (await nav.nth(1).inner_text()).strip()
            bankod = bank_try
            startdatum = int(swedish_date_to_yyyymmdd(date_text))

        # B: “Färjestad 2025-09-01” som sammanhängande sträng
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
                bank_try = extract_bankod_from_text(track_part)
                bankod = bank_try or track_to_bankod(track_part)
                logging.info("  bana-text: '%s' -> bankod=%s", track_part, bankod)
                break

        if bankod is None or startdatum is None:
            logging.info("  kunde inte hitta bana/datum")
            await browser.close(); return []
        else:
            logging.info("  hittade bana=%s datum=%s", bankod, startdatum)

        # 2) Propositionnummer (“Prop. X”)
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

        # 3) Hästnamn
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

class Command(BaseCommand):  
    help = "Scrape proposition-sidor till tabellen 'proposition'."  

    DAY_START_ID     = 610_128        # första raceday-id att prova  
    DAY_END_ID       = 610_300        # sista raceday-id att prova   
    PROP_START_ID    = 720_144        # start på proposition-ts      
    PROP_END_ID      = 722_900        # stopp på proposition-ts      
    PROP_STOP_STREAK = 5              # efter 5 tomma i rad -> nästa raceday  

    def handle(self, *args, **opts):  
        base = "https://sportapp.travsport.se/propositions/raceday/ts{}/proposition/ts{}"
        total = 0
        prop_id = self.PROP_START_ID  # behålls löpande över dagar  

        for day_id in range(self.DAY_START_ID, self.DAY_END_ID + 1):  
            empty_streak = 0
            inserted_for_day = 0
            logging.info("=== Raceday ts%d ===", day_id)  

            while prop_id <= self.PROP_END_ID and empty_streak < self.PROP_STOP_STREAK:  
                url = base.format(day_id, prop_id)
                logging.info("Scraping %s", url)
                try:
                    rows = asyncio.run(scrape_proposition_page(url))
                except Exception as exc:
                    logging.warning("  failed: %s", exc)
                    rows = []

                if not rows:
                    empty_streak += 1
                    logging.info("  no rows (streak=%d)", empty_streak)
                else:
                    empty_streak = 0
                    for r in rows:
                        Proposition.objects.update_or_create(
                            startdatum=r.startdatum, bankod=r.bankod,
                            namn=r.namn, proposition=r.proposition,
                            defaults={},
                        )
                    cnt = len(rows)
                    inserted_for_day += cnt
                    total += cnt
                    logging.info("  inserted/updated %d rows", cnt)

                prop_id += 1  # öka alltid prop-id  

            logging.info("=== Done day ts%d: inserted=%d, next prop_id=%d ===",
                         day_id, inserted_for_day, prop_id)  

            if prop_id > self.PROP_END_ID:  
                break  # slut på proposition-id

        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows processed."))  
