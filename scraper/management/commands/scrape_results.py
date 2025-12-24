import asyncio, re, unicodedata, logging
from dataclasses import dataclass
from typing import List, Tuple, Optional 
from django.core.management.base import BaseCommand
from django.db import IntegrityError 
from playwright.async_api import async_playwright, Error as PlaywrightError
from scraper.models import HorseResult

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}

def swedish_date_to_yyyymmdd(text: str) -> str:
    parts = (text or "").strip().upper().split()   
    if len(parts) == 4:
        _, d, m, y = parts
    else:
        d, m, y = parts
    return f"{int(y):04d}{SWEDISH_MONTH[m]:02d}{int(d):02d}"

_APOSTROPHES_RE = re.compile(r"[\'\u2019]")    

def normalize_cell_text(s: str) -> str:  
    if s is None:  
        return ""  
    return s.replace("\u00a0", " ").strip()   

def trim_to_max(s: str, max_len: int) -> str:   
    s = s or ""  
    return s if len(s) <= max_len else s[:max_len]   

def normalize_name(name: str) -> str:  
    cleaned = normalize_cell_text(name).replace("*", "")   
    cleaned = _APOSTROPHES_RE.sub("", cleaned) 
    cleaned = re.sub(r"\s+", " ", cleaned).strip()   
    return trim_to_max(cleaned, 50)   

def normalize_kusk(kusk: str) -> str:   
    cleaned = re.sub(r"\s+", " ", normalize_cell_text(kusk)).strip()  
    cleaned = _APOSTROPHES_RE.sub("", cleaned) 
    return trim_to_max(cleaned, 80)   

def sanitize_underlag(raw: str) -> str:  
    t = normalize_cell_text(raw).lower()  
    if not t:  
        return ""  
    t = t.replace("(", "").replace(")", "")  
    t = re.sub(r"\s+", "", t)  
    t = re.sub(r"[^a-z]", "", t)  
    return t[:2]  


dist_slash_re = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{3,4})\s*([a-zA-Z() \u00a0]*)\s*$", re.I)  
dist_colon_re = re.compile(r"^\s*(\d{3,4})\s*:\s*(\d{1,2})\s*$", re.I)  
dist_only_re = re.compile(r"^\s*(\d{3,4})\s*(?:m)?\s*$", re.I)  

def parse_dist_spar(txt: str):  
    t = normalize_cell_text(txt)  
    if not t:  
        return None, None, ""  

    m = dist_slash_re.match(t)  
    if m:  
        spar = int(m.group(1))  
        distans = int(m.group(2))  
        underlag = sanitize_underlag(m.group(3))  
        return distans, spar, underlag  

    m = dist_colon_re.match(t)  
    if m:  
        distans = int(m.group(1))  
        spar = int(m.group(2))  
        return distans, spar, ""  

    m = dist_only_re.match(t)  
    if m:  
        distans = int(m.group(1))  
        return distans, 1, ""  

    return None, None, ""  


placering_with_r = re.compile(r"^(\d{1,2})r$", re.I)  

def map_placering_value(raw: str):  
    t = normalize_cell_text(raw).lower()  
    if not t:  
        return None  

    token = re.split(r"\s+", t, 1)[0]  
    token = re.sub(r"[^0-9a-zåäö]", "", token)  
    if not token:  
        return None  

    mr = placering_with_r.match(token)  
    if mr:  
        token = mr.group(1)  

    if token in ("k", "p", "str", "d"): 
        return 99 

    if not token.isdigit() or len(token) > 2: 
        return None 

    try: 
        v = int(token) 
    except ValueError: 
        return None 

    if v == 0 or v == 9: 
        return 15 

    return v 


TIME_VALUE = re.compile(r"(?:\d+\.)?(\d{1,2})[.,](\d{1,2})") 

def parse_tid_cell(raw: str): 
    t = normalize_cell_text(raw).lower() 
    if not t: 
        return None, "", "" 

    t2 = re.sub(r"[()\s]", "", t) 

    letters = re.sub(r"[0-9\.,]", "", t2) 
    startmetod = "a" if "a" in letters else "" 
    galopp = "g" if "g" in letters else "" 

    force99 = ("dist" in letters) or ("kub" in letters) or ("vmk" in letters) or ("u" in letters) or ("d" in letters) 

    tid = None 
    m = TIME_VALUE.search(t2) 
    if m: 
        try: 
            tid = float(f"{m.group(1)}.{m.group(2)}") 
        except ValueError: 
            tid = None 

    if force99: 
        return 99.0, startmetod, galopp 

    if tid is None: 
        has_sep = ("," in t2) or ("." in t2) 
        digits = re.sub(r"\D+", "", t2) 
        if (not has_sep) and digits and len(digits) <= 2 and letters: 
            return 99.0, startmetod, galopp 

    return tid, startmetod, galopp 

PRIS_LINE_RE = re.compile(r"\bPris\s*:\s*(.+?)\bkr\b", re.IGNORECASE | re.DOTALL) 
PRISPLACERADE_RE = re.compile(r"\((\d+)\s*prisplacerade\)", re.IGNORECASE) 
LAGST_RE = re.compile(r"Lägst\s+([0-9][0-9\.\s\u00a0]*)\s*kr", re.IGNORECASE) 

def _parse_swe_int(token: str) -> Optional[int]: 
    if token is None: 
        return None 
    t = normalize_cell_text(token) 
    t = t.replace("(", "").replace(")", "") 
    t = t.replace("\u00a0", " ") 
    t = t.replace(".", "").replace(" ", "") 
    t = re.sub(r"[^\d]", "", t) 
    if not t: 
        return None 
    try: 
        return int(t) 
    except ValueError: 
        return None 

def parse_pris_text(full_text: str) -> Tuple[List[int], Optional[int], Optional[int]]: 
    text = normalize_cell_text(full_text) 
    if not text: 
        return [], None, None 

    m = PRIS_LINE_RE.search(text) 
    if not m: 
        return [], _parse_swe_int(LAGST_RE.search(text).group(1)) if LAGST_RE.search(text) else None, None 

    prize_part = normalize_cell_text(m.group(1)) 

    prizes: List[int] = [] 
    for raw_tok in prize_part.split("-"): 
        v = _parse_swe_int(raw_tok) 
        if v is None: 
            continue 
        prizes.append(v) 

    pn = None 
    mp = PRISPLACERADE_RE.search(text) 
    if mp: 
        try: 
            pn = int(mp.group(1)) 
        except ValueError: 
            pn = None 

    min_pris = None 
    ml = LAGST_RE.search(text) 
    if ml: 
        min_pris = _parse_swe_int(ml.group(1)) 

    return prizes, min_pris, pn 

def pris_for_placering(placering: Optional[int], prizes: List[int], min_pris: Optional[int]) -> int: 
    if placering is None or placering == 99 or placering <= 0: 
        return 0 
    if prizes: 
        if placering <= len(prizes): 
            return prizes[placering - 1] 
        if min_pris is not None: 
            return int(min_pris) 
    return 0 


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
    kusk: str
    pris: int
    odds: Optional[int] = None



async def _extract_pris_text_from_section(section) -> str: 
    try: 
        loc = section.get_by_text(re.compile(r"\bPris\s*:", re.I)) 
        if await loc.count() > 0: 
            return normalize_cell_text(await loc.first.inner_text()) 
    except Exception: 
        pass 

    try: 
        loc = section.locator("xpath=.//*[contains(., 'Pris:') or contains(., 'PRIS:')]") 
        if await loc.count() > 0: 
            return normalize_cell_text(await loc.first.inner_text()) 
    except Exception: 
        pass 

    return "" 

async def scrape_page(page, url: str) -> List[Row]: 
    logging.info("  goto %s", url) 
    await page.goto(url, timeout=60_000, wait_until="domcontentloaded") 
    logging.info("  landed %s", page.url)

    logging.info("  waiting for grid...") 
    await page.wait_for_selector("div[role='row'][data-rowindex]", timeout=15_000) 
    logging.info("  grid found") 

    nav = page.locator("div[class*='RaceDayNavigator_title'] span")
    if await nav.count() < 2:
        return []

    track_raw = normalize_cell_text(await nav.nth(0).inner_text())
    bankod = track_to_bankod(track_raw)

    date_txt = normalize_cell_text(await nav.nth(1).inner_text())
    datum = int(swedish_date_to_yyyymmdd(date_txt))

    data: List[Row] = []
    lopp_headers = page.locator("//h2[starts-with(normalize-space(),'Lopp')]")
    header_count = await lopp_headers.count()
    logging.info("  found %d lopp headers", header_count) 

    for i in range(header_count):
        header = lopp_headers.nth(i)
        await header.scroll_into_view_if_needed() 
        m = re.search(r"Lopp\s+(\d+)", normalize_cell_text(await header.inner_text()))
        if not m:
            continue
        lopp = int(m.group(1))

        section = header.locator("xpath=ancestor::div[contains(@class,'MuiBox-root')][1]")

        pris_text = await _extract_pris_text_from_section(section) 
        prizes, min_pris, _ = parse_pris_text(pris_text) 

        rows = await section.locator("div[role='row'][data-rowindex]").all()
        if not rows:
            continue

        for row in rows:
            cell = lambda f: row.locator(f"div[data-field='{f}']")

            nr_txt = normalize_cell_text(await cell("horse").locator("div").first.inner_text())
            nr_m = re.search(r"\d+", nr_txt)
            if not nr_m:
                continue
            nr = int(nr_m.group(0))

            namn_raw = normalize_cell_text(await cell("horse").locator("span").first.inner_text())
            namn = normalize_name(namn_raw.split("(")[0])

            kusk = ""
            try:
                drv = cell("driver")
                a = drv.locator("a")
                kusk_raw = (await a.first.inner_text()).strip() if await a.count() > 0 else normalize_cell_text(await drv.inner_text())
                kusk = normalize_kusk(kusk_raw) 
            except Exception:
                kusk = ""

            placetxt = normalize_cell_text(await cell("placementDisplay").inner_text())
            placering = map_placering_value(placetxt)

            dist_raw = normalize_cell_text(await cell("startPositionAndDistance").inner_text())
            distans, spar, underlag = parse_dist_spar(dist_raw)

            tid_raw = normalize_cell_text(await cell("time").inner_text())
            tid, startmetod, galopp = parse_tid_cell(tid_raw)

            pris = pris_for_placering(placering, prizes, min_pris)

            odds = None 
            try: 
                odds_cell = cell("odds") 
                if await odds_cell.count() > 0: 
                    odds_txt = normalize_cell_text(await odds_cell.inner_text()) 
                    mm = re.search(r"\d+", odds_txt) 
                    if mm: 
                        odds = int(mm.group(0)) 
            except Exception: 
                odds = None 

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
                kusk=kusk,
                pris=pris,
                odds=odds,
            ))

    return data



def write_rows_to_db(rows: List[Row]) -> int: 
    created_n = 0 
    updated_n = 0 
    unchanged_n = 0 

    for r in rows: 
        namn_clean = normalize_name(r.namn) 

        try: 
            obj, created = HorseResult.objects.get_or_create( 
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
                    kusk=normalize_kusk(r.kusk), 
                    pris=r.pris,
                    odds=(r.odds if (r.odds not in (None, 999)) else 999),
                ),
            )
        except IntegrityError as e: 
            logging.exception("DB IntegrityError for (%s,%s,L%s,%s): %s", r.datum, r.bankod, r.lopp, namn_clean, e) 
            continue 

        if created: 
            created_n += 1 
            continue 

        changed_fields = [] 

        if obj.nr != r.nr:
            obj.nr = r.nr
            changed_fields.append("nr")

        if obj.distans != r.distans:
            obj.distans = r.distans
            changed_fields.append("distans")

        if obj.spar != r.spar:
            obj.spar = r.spar
            changed_fields.append("spar")

        if obj.placering != r.placering:
            obj.placering = r.placering
            changed_fields.append("placering")

        if obj.tid != r.tid:
            obj.tid = r.tid
            changed_fields.append("tid")

        if obj.startmetod != (r.startmetod or ""):
            obj.startmetod = (r.startmetod or "")
            changed_fields.append("startmetod")

        if obj.galopp != (r.galopp or ""):
            obj.galopp = (r.galopp or "")
            changed_fields.append("galopp")

        if obj.underlag != (r.underlag or ""):
            obj.underlag = (r.underlag or "")
            changed_fields.append("underlag")

        kusk_clean = normalize_kusk(r.kusk) 
        if obj.kusk != (kusk_clean or ""): 
            obj.kusk = (kusk_clean or "") 
            changed_fields.append("kusk") 

        if obj.pris != r.pris:
            obj.pris = r.pris
            changed_fields.append("pris")

        incoming_odds = r.odds
        existing_odds = obj.odds if obj.odds is not None else 999
        if incoming_odds not in (None, 999) and existing_odds == 999:
            if obj.odds != int(incoming_odds):
                obj.odds = int(incoming_odds)
                changed_fields.append("odds")

        if changed_fields:
            obj.save(update_fields=changed_fields)
            updated_n += 1
        else:
            unchanged_n += 1

    logging.info("  db_created=%d db_updated=%d db_unchanged=%d", created_n, updated_n, unchanged_n) 
    return created_n + updated_n 



async def run_range(start_id: int, end_id: int) -> int: 
    base = "https://sportapp.travsport.se/race/raceday/ts{}/results/all" 
    total_scraped = 0 

    async with async_playwright() as p: 
        browser = await p.chromium.launch(headless=True) 
        ctx = await browser.new_context() 
        ctx.set_default_timeout(120_000) 
        page = await ctx.new_page() 

        try: 
            for ts_id in range(start_id, end_id + 1): 
                url = base.format(ts_id) 
                logging.info("Scraping %s", url) 

                try: 
                    rows = await scrape_page(page, url) 
                except PlaywrightError as exc: 
                    logging.warning("  failed: %s", exc) 
                    continue 
                except Exception as exc: 
                    logging.warning("  failed: %s", exc) 
                    continue 

                logging.info("  scraped_rows=%d", len(rows)) 
                if not rows:
                    continue

                total_scraped += len(rows) 

                await asyncio.to_thread(write_rows_to_db, rows) 

        finally: 
            await ctx.close() 
            await browser.close() 

    return total_scraped 

class Command(BaseCommand):
    help = "Scrape hard-coded ts-ID range into Result"
    
    #START_ID = 600_569
    #END_ID = 601_432
    
    START_ID = 610_405
    END_ID = 610_435
    
    #605_589 buggar wtf? Pris och grandprix? 
    #Slutade tts605584
    
    # Första januari 2024 ID: 605104
    # Sista december 2024 ID: 605919
    # 1 januari 2025 ID: 609600
    
    # Fösta januari 2023 ts600569
    # Sista decemer 2023 ts601432

    def handle(self, *args, **opts):
        total = asyncio.run(run_range(self.START_ID, self.END_ID)) 
        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows scraped & processed.")) 
