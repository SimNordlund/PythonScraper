import asyncio, re, unicodedata, logging
from dataclasses import dataclass
from typing import List, Tuple, Optional  # //Changed!
from django.core.management.base import BaseCommand
from django.db import IntegrityError  # //Changed!
from playwright.async_api import async_playwright, Error as PlaywrightError
from scraper.models import HorseResult

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}

def swedish_date_to_yyyymmdd(text: str) -> str:
    parts = (text or "").strip().upper().split()  # //Changed!
    if len(parts) == 4:
        _, d, m, y = parts
    else:
        d, m, y = parts
    return f"{int(y):04d}{SWEDISH_MONTH[m]:02d}{int(d):02d}"


# ---------------------------
# Normalisering / parsing
# ---------------------------

_APOSTROPHES_RE = re.compile(r"[\'\u2019]")  # //Changed!  # ' and ’ (right single quotation mark)

def normalize_cell_text(s: str) -> str:  # //Changed!
    if s is None:  # //Changed!
        return ""  # //Changed!
    return s.replace("\u00a0", " ").strip()  # //Changed!

def trim_to_max(s: str, max_len: int) -> str:  # //Changed!
    s = s or ""  # //Changed!
    return s if len(s) <= max_len else s[:max_len]  # //Changed!

def normalize_name(name: str) -> str:  # //Changed!
    cleaned = normalize_cell_text(name).replace("*", "")  # //Changed!
    cleaned = _APOSTROPHES_RE.sub("", cleaned)  # //Changed!  # remove ' and ’
    cleaned = re.sub(r"\s+", " ", cleaned).strip()  # //Changed!
    return trim_to_max(cleaned, 50)  # //Changed!

def normalize_kusk(kusk: str) -> str:  # //Changed!
    cleaned = re.sub(r"\s+", " ", normalize_cell_text(kusk)).strip()  # //Changed!
    cleaned = _APOSTROPHES_RE.sub("", cleaned)  # //Changed!  # remove ' and ’
    return trim_to_max(cleaned, 80)  # //Changed!

def sanitize_underlag(raw: str) -> str:  # //Changed!
    t = normalize_cell_text(raw).lower()  # //Changed!
    if not t:  # //Changed!
        return ""  # //Changed!
    t = t.replace("(", "").replace(")", "")  # //Changed!
    t = re.sub(r"\s+", "", t)  # //Changed!
    t = re.sub(r"[^a-z]", "", t)  # //Changed!
    return t[:2]  # //Changed!


dist_slash_re = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{3,4})\s*([a-zA-Z() \u00a0]*)\s*$", re.I)  # //Changed!
dist_colon_re = re.compile(r"^\s*(\d{3,4})\s*:\s*(\d{1,2})\s*$", re.I)  # //Changed!
dist_only_re = re.compile(r"^\s*(\d{3,4})\s*(?:m)?\s*$", re.I)  # //Changed!

def parse_dist_spar(txt: str):  # //Changed!
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
        return distans, 1, ""  # //Changed!

    return None, None, ""  # //Changed!


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


TIME_VALUE = re.compile(r"(?:\d+\.)?(\d{1,2})[.,](\d{1,2})")  # //Changed!

def parse_tid_cell(raw: str):  # //Changed!
    t = normalize_cell_text(raw).lower()  # //Changed!
    if not t:  # //Changed!
        return None, "", ""  # //Changed!

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

    if tid is None:  # //Changed!
        has_sep = ("," in t2) or ("." in t2)  # //Changed!
        digits = re.sub(r"\D+", "", t2)  # //Changed!
        if (not has_sep) and digits and len(digits) <= 2 and letters:  # //Changed!
            return 99.0, startmetod, galopp  # //Changed!

    return tid, startmetod, galopp  # //Changed!


# ---------------------------
# Pris
# ---------------------------

PRIS_LINE_RE = re.compile(r"\bPris\s*:\s*(.+?)\bkr\b", re.IGNORECASE | re.DOTALL)  # //Changed!
PRISPLACERADE_RE = re.compile(r"\((\d+)\s*prisplacerade\)", re.IGNORECASE)  # //Changed!
LAGST_RE = re.compile(r"Lägst\s+([0-9][0-9\.\s\u00a0]*)\s*kr", re.IGNORECASE)  # //Changed!

def _parse_swe_int(token: str) -> Optional[int]:  # //Changed!
    if token is None:  # //Changed!
        return None  # //Changed!
    t = normalize_cell_text(token)  # //Changed!
    t = t.replace("(", "").replace(")", "")  # //Changed!
    t = t.replace("\u00a0", " ")  # //Changed!
    t = t.replace(".", "").replace(" ", "")  # //Changed!
    t = re.sub(r"[^\d]", "", t)  # //Changed!
    if not t:  # //Changed!
        return None  # //Changed!
    try:  # //Changed!
        return int(t)  # //Changed!
    except ValueError:  # //Changed!
        return None  # //Changed!

def parse_pris_text(full_text: str) -> Tuple[List[int], Optional[int], Optional[int]]:  # //Changed!
    text = normalize_cell_text(full_text)  # //Changed!
    if not text:  # //Changed!
        return [], None, None  # //Changed!

    m = PRIS_LINE_RE.search(text)  # //Changed!
    if not m:  # //Changed!
        return [], _parse_swe_int(LAGST_RE.search(text).group(1)) if LAGST_RE.search(text) else None, None  # //Changed!

    prize_part = normalize_cell_text(m.group(1))  # //Changed!

    prizes: List[int] = []  # //Changed!
    for raw_tok in prize_part.split("-"):  # //Changed!
        v = _parse_swe_int(raw_tok)  # //Changed!
        if v is None:  # //Changed!
            continue  # //Changed!
        prizes.append(v)  # //Changed!

    pn = None  # //Changed!
    mp = PRISPLACERADE_RE.search(text)  # //Changed!
    if mp:  # //Changed!
        try:  # //Changed!
            pn = int(mp.group(1))  # //Changed!
        except ValueError:  # //Changed!
            pn = None  # //Changed!

    min_pris = None  # //Changed!
    ml = LAGST_RE.search(text)  # //Changed!
    if ml:  # //Changed!
        min_pris = _parse_swe_int(ml.group(1))  # //Changed!

    return prizes, min_pris, pn  # //Changed!

def pris_for_placering(placering: Optional[int], prizes: List[int], min_pris: Optional[int]) -> int:  # //Changed!
    if placering is None or placering == 99 or placering <= 0:  # //Changed!
        return 0  # //Changed!
    if prizes:  # //Changed!
        if placering <= len(prizes):  # //Changed!
            return prizes[placering - 1]  # //Changed!
        if min_pris is not None:  # //Changed!
            return int(min_pris)  # //Changed!
    return 0  # //Changed!


# ---------------------------
# Bankod
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
    kusk: str
    pris: int
    odds: Optional[int] = None


# ---------------------------
# Scrape one page (async)
# ---------------------------

async def _extract_pris_text_from_section(section) -> str:  # //Changed!
    try:  # //Changed!
        loc = section.get_by_text(re.compile(r"\bPris\s*:", re.I))  # //Changed!
        if await loc.count() > 0:  # //Changed!
            return normalize_cell_text(await loc.first.inner_text())  # //Changed!
    except Exception:  # //Changed!
        pass  # //Changed!

    try:  # //Changed!
        loc = section.locator("xpath=.//*[contains(., 'Pris:') or contains(., 'PRIS:')]")  # //Changed!
        if await loc.count() > 0:  # //Changed!
            return normalize_cell_text(await loc.first.inner_text())  # //Changed!
    except Exception:  # //Changed!
        pass  # //Changed!

    return ""  # //Changed!

async def scrape_page(page, url: str) -> List[Row]:  # //Changed!
    logging.info("  goto %s", url)  # //Changed!
    await page.goto(url, timeout=60_000, wait_until="domcontentloaded")  # //Changed!
    logging.info("  landed %s", page.url)

    logging.info("  waiting for grid...")  # //Changed!
    await page.wait_for_selector("div[role='row'][data-rowindex]", timeout=15_000)  # //Changed!
    logging.info("  grid found")  # //Changed!

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
    logging.info("  found %d lopp headers", header_count)  # //Changed!

    for i in range(header_count):
        header = lopp_headers.nth(i)
        await header.scroll_into_view_if_needed()  # //Changed!
        m = re.search(r"Lopp\s+(\d+)", normalize_cell_text(await header.inner_text()))
        if not m:
            continue
        lopp = int(m.group(1))

        section = header.locator("xpath=ancestor::div[contains(@class,'MuiBox-root')][1]")

        pris_text = await _extract_pris_text_from_section(section)  # //Changed!
        prizes, min_pris, _ = parse_pris_text(pris_text)  # //Changed!

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
            namn = normalize_name(namn_raw.split("(")[0])  # //Changed! (removes apostrophes inside normalize_name)

            kusk = ""
            try:
                drv = cell("driver")
                a = drv.locator("a")
                kusk_raw = (await a.first.inner_text()).strip() if await a.count() > 0 else normalize_cell_text(await drv.inner_text())
                kusk = normalize_kusk(kusk_raw)  # //Changed! (removes apostrophes inside normalize_kusk)
            except Exception:
                kusk = ""

            placetxt = normalize_cell_text(await cell("placementDisplay").inner_text())
            placering = map_placering_value(placetxt)

            dist_raw = normalize_cell_text(await cell("startPositionAndDistance").inner_text())
            distans, spar, underlag = parse_dist_spar(dist_raw)

            tid_raw = normalize_cell_text(await cell("time").inner_text())
            tid, startmetod, galopp = parse_tid_cell(tid_raw)

            pris = pris_for_placering(placering, prizes, min_pris)

            odds = None  # //Changed!
            try:  # //Changed!
                odds_cell = cell("odds")  # //Changed!
                if await odds_cell.count() > 0:  # //Changed!
                    odds_txt = normalize_cell_text(await odds_cell.inner_text())  # //Changed!
                    mm = re.search(r"\d+", odds_txt)  # //Changed!
                    if mm:  # //Changed!
                        odds = int(mm.group(0))  # //Changed!
            except Exception:  # //Changed!
                odds = None  # //Changed!

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


# ---------------------------
# DB write (sync) - körs i thread
# ---------------------------

def write_rows_to_db(rows: List[Row]) -> int:  # //Changed!
    created_n = 0  # //Changed!
    updated_n = 0  # //Changed!
    unchanged_n = 0  # //Changed!

    for r in rows:  # //Changed!
        # r.namn är redan normalize_name() från scrape_page,
        # men vi kör igen för säkerhets skull (idempotent).  # //Changed!
        namn_clean = normalize_name(r.namn)  # //Changed!

        try:  # //Changed!
            obj, created = HorseResult.objects.get_or_create(  # //Changed!
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
                    kusk=normalize_kusk(r.kusk),  # //Changed!
                    pris=r.pris,
                    odds=(r.odds if (r.odds not in (None, 999)) else 999),
                ),
            )
        except IntegrityError as e:  # //Changed!
            logging.exception("DB IntegrityError for (%s,%s,L%s,%s): %s", r.datum, r.bankod, r.lopp, namn_clean, e)  # //Changed!
            continue  # //Changed!

        if created:  # //Changed!
            created_n += 1  # //Changed!
            continue  # //Changed!

        changed_fields = []  # //Changed!

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

        kusk_clean = normalize_kusk(r.kusk)  # //Changed!
        if obj.kusk != (kusk_clean or ""):  # //Changed!
            obj.kusk = (kusk_clean or "")  # //Changed!
            changed_fields.append("kusk")  # //Changed!

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

    logging.info("  db_created=%d db_updated=%d db_unchanged=%d", created_n, updated_n, unchanged_n)  # //Changed!
    return created_n + updated_n  # //Changed!


# ---------------------------
# Main async runner
# ---------------------------

async def run_range(start_id: int, end_id: int) -> int:  # //Changed!
    base = "https://sportapp.travsport.se/race/raceday/ts{}/results/all"  # //Changed!
    total_scraped = 0  # //Changed!

    async with async_playwright() as p:  # //Changed!
        browser = await p.chromium.launch(headless=True)  # //Changed!
        ctx = await browser.new_context()  # //Changed!
        ctx.set_default_timeout(120_000)  # //Changed!
        page = await ctx.new_page()  # //Changed!

        try:  # //Changed!
            for ts_id in range(start_id, end_id + 1):  # //Changed!
                url = base.format(ts_id)  # //Changed!
                logging.info("Scraping %s", url)  # //Changed!

                try:  # //Changed!
                    rows = await scrape_page(page, url)  # //Changed!
                except PlaywrightError as exc:  # //Changed!
                    logging.warning("  failed: %s", exc)  # //Changed!
                    continue  # //Changed!
                except Exception as exc:  # //Changed!
                    logging.warning("  failed: %s", exc)  # //Changed!
                    continue  # //Changed!

                logging.info("  scraped_rows=%d", len(rows))  # //Changed!
                if not rows:
                    continue

                total_scraped += len(rows)  # //Changed!

                await asyncio.to_thread(write_rows_to_db, rows)  # //Changed!

        finally:  # //Changed!
            await ctx.close()  # //Changed!
            await browser.close()  # //Changed!

    return total_scraped  # //Changed!


# ---------------------------
# Django management command
# ---------------------------

class Command(BaseCommand):
    help = "Scrape hard-coded ts-ID range into Result"

    START_ID = 610_375
    END_ID = 610_420

    def handle(self, *args, **opts):
        total = asyncio.run(run_range(self.START_ID, self.END_ID))  # //Changed!
        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows scraped & processed."))  # //Changed!
