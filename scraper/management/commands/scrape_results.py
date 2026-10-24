import asyncio
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone
from playwright.async_api import Error as PlaywrightError

from scraper.models import HorseResult
from scraper.polite import ScrapePolicy, ScrapeStopped

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


SWEDISH_MONTH = {
    "JANUARI": 1, "FEBRUARI": 2, "MARS": 3, "APRIL": 4, "MAJ": 5,
    "JUNI": 6, "JULI": 7, "AUGUSTI": 8, "SEPTEMBER": 9, "OKTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}
SWEDISH_MONTH_BY_NUMBER = {v: k.lower() for k, v in SWEDISH_MONTH.items()}
CALENDAR_URL = "https://sportapp.travsport.se/race/calendar/race?year={year}&competition=SPORT&month={month:02d}"

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
    value = normalize_cell_text(raw).lower().strip()
    if not value:
        return ""
    if "vinterbana" in value:
        return "v"
    if "något tung" in value:
        return "n"
    if "tung" in value:
        return "t"
    return ""

async def _extract_banforhallande_value_from_section(section) -> str:
    try:
        val = section.locator("span:has-text('Banförhållande:') + span")
        if await val.count() > 0:
            return normalize_cell_text(await val.first.inner_text())

        label = section.locator("span:has-text('Banförhållande')")
        if await label.count() > 0:
            sib = label.first.locator("xpath=following-sibling::span[1]")
            if await sib.count() > 0:
                return normalize_cell_text(await sib.first.inner_text())
    except PlaywrightError as exc:
        logger.warning("Could not read track conditions: %s", exc)
    return ""


dist_slash_re = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{3,4})\s*([a-zA-Z() \u00a0]*)\s*$", re.IGNORECASE)
dist_colon_re = re.compile(r"^\s*(\d{3,4})\s*:\s*(\d{1,2})\s*$", re.IGNORECASE)
dist_only_re = re.compile(r"^\s*(\d{3,4})\s*(?:m)?\s*$", re.IGNORECASE)

def parse_dist_spar(txt: str):
    t = normalize_cell_text(txt)
    if not t:
        return None, None, ""

    m = dist_slash_re.match(t)
    if m:
        spar = int(m.group(1))
        distans = int(m.group(2))
        return distans, spar, ""

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

placering_with_r = re.compile(r"^(\d{1,2})r$", re.IGNORECASE)

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

    if token in ("k", "p", "str"):
        return 99
    
    if token in ("d"):
        return 15

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

    force99 = ("dist" in letters) or ("kub" in letters) or ("vmk" in letters) or ("u" in letters) or ("d" in letters) or ("it" in letters)

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


PRIS_PREFIX_RE = re.compile(r"\bPris\s*:\s*", re.IGNORECASE)
DASH_CHARS = r"\-\u2010\u2011\u2012\u2013\u2014\u2212\uFE63\uFF0D"

LEADING_PRIZES_RE = re.compile(
    rf"^\s*([0-9][0-9\.\s\u00a0]*(?:\s*[{DASH_CHARS}]\s*[0-9][0-9\.\s\u00a0]*)*)"
)
NUMBER_TOKEN_RE = re.compile(r"\d{1,3}(?:[.\s\u00a0]\d{3})+|\d+")
SAMT_TILL_OVRIGA_RE = re.compile(
    r"\bsamt\s+([0-9][0-9\.\s\u00a0]*)\s*kr\s+till\s+övriga\b",
    re.IGNORECASE
)
PRISPLACERADE_RE = re.compile(r"\((\d+)\s*prisplacerade\)", re.IGNORECASE)
LAGST_RE = re.compile(r"Lägst\s+([0-9][0-9\.\s\u00a0]*)\s*kr", re.IGNORECASE)

def _parse_swe_int(token: str) -> int | None:
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

def parse_pris_text(full_text: str) -> tuple[list[int], int | None, int | None]:
    text = normalize_cell_text(full_text)
    if not text:
        return [], None, None

    m0 = PRIS_PREFIX_RE.search(text)
    if not m0:
        ml0 = LAGST_RE.search(text)
        return [], _parse_swe_int(ml0.group(1)) if ml0 else None, None

    after = text[m0.end():]

    prizes: list[int] = []
    m1 = LEADING_PRIZES_RE.match(after)
    if m1:
        leading = m1.group(1)
        for raw_tok in NUMBER_TOKEN_RE.findall(leading):
            v = _parse_swe_int(raw_tok)
            if v is not None:
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

    mo = SAMT_TILL_OVRIGA_RE.search(text)
    if mo:
        extra = _parse_swe_int(mo.group(1))
        if extra is not None:
            min_pris = extra if min_pris is None else max(min_pris, extra)

    return prizes, min_pris, pn

def pris_for_lopp(prizes: list[int], min_pris: int | None) -> int:
    if prizes:
        return int(prizes[0])
    return int(min_pris) if min_pris is not None else 0


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


MONTHS_PATTERN = "|".join(SWEDISH_MONTH.keys())  
DATE_PART_RX = re.compile(rf"\b(\d{{1,2}})\s+({MONTHS_PATTERN})\s+(\d{{4}})\b", re.IGNORECASE)
WEEKDAYS = ("MÅNDAG","TISDAG","ONSDAG","TORSDAG","FREDAG","LÖRDAG","SÖNDAG")  
WEEKDAYS_RX = re.compile(rf"\b(?:{'|'.join(WEEKDAYS)})\b", re.IGNORECASE)

async def _get_nav_texts(page):  
    for sel in ("[class*='RaceDayNavigator'] span", "header span"):  
        loc = page.locator(sel)  
        n = await loc.count()  
        texts = []  
        for i in range(n):  
            t = normalize_cell_text(await loc.nth(i).inner_text())  
            if t:  
                texts.append(t)  
        if texts:  
            return texts  
    return []  

def _extract_track_and_date(texts):  
    cleaned = [t for t in (texts or []) if t and t.strip()]
    cleaned = [t.strip() for t in cleaned]

    date_container = None
    date_part = None

    for t in cleaned:
        m = DATE_PART_RX.search(t.upper())
        if m:
            date_container = t
            date_part = m.group(0).upper()
            break

    if not date_part:
        return None, None

    track_txt = None
    for t in cleaned:
        if t == date_container:
            continue
        if re.search(r"\d", t):
            continue
        up = t.upper()
        if up in ("STARTLISTA", "DAGSRESULTAT", "TÄVLINGSDAGSRESULTAT"):
            continue
        track_txt = up
        break

    if not track_txt and date_container:
        up = date_container.upper()
        up = up.replace(date_part, " ")
        up = WEEKDAYS_RX.sub(" ", up)

        up = up.strip()
        for prefix in ("TÄVLINGSDAG", "TRAVTÄVLING", "DAG"):
            if up.startswith(prefix):
                up = up[len(prefix):].strip()
        track_txt = re.sub(r"\s+", " ", up).strip()

    return track_txt, date_part


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
    odds: int | None = None


async def _extract_pris_text_from_section(section) -> str:
    try:
        loc = section.get_by_text(re.compile(r"\bPris\s*:", re.IGNORECASE))
        if await loc.count() > 0:
            return normalize_cell_text(await loc.first.inner_text())
    except PlaywrightError as exc:
        logger.debug("Prize text lookup failed; trying fallback selector: %s", exc)

    try:
        loc = section.locator("xpath=.//*[contains(., 'Pris:') or contains(., 'PRIS:')]")
        if await loc.count() > 0:
            return normalize_cell_text(await loc.first.inner_text())
    except PlaywrightError as exc:
        logger.warning("Could not read prize text with fallback selector: %s", exc)

    return ""


async def scrape_page(page, url: str, policy: ScrapePolicy) -> list[Row]:
    try:
        await policy.goto(page, url)
    except PlaywrightError:
        return []

    try:
        await policy.wait_for_selector(page, "div[role='row'][data-rowindex]", timeout=120_000)
        await policy.wait_for_selector(page, "xpath=//h2[starts-with(normalize-space(),'Lopp')]", timeout=120_000)
    except PlaywrightError:
        return []

    texts = await _get_nav_texts(page)  
    track_raw, date_txt = _extract_track_and_date(texts)  
    if not track_raw or not date_txt:  
        logger.info("Nav parse failed. texts=%s", texts)
        return []  

    bankod = track_to_bankod(track_raw)  
    datum = int(swedish_date_to_yyyymmdd(date_txt))  

    data: list[Row] = []
    lopp_headers = page.locator("//h2[starts-with(normalize-space(),'Lopp')]")
    header_count = await lopp_headers.count()

    for i in range(header_count):
        policy.check()
        header = lopp_headers.nth(i)
        await header.scroll_into_view_if_needed()
        m = re.search(r"Lopp\s+(\d+)", normalize_cell_text(await header.inner_text()))
        if not m:
            continue
        lopp = int(m.group(1))

        info_section = header.locator("xpath=ancestor::div[contains(@class,'MuiBox-root')][1]")  

        grid = header.locator("xpath=following::div[contains(@class,'MuiDataGrid-root')][1]")  

        pris_text = await _extract_pris_text_from_section(info_section)  
        prizes, min_pris, _ = parse_pris_text(pris_text)
        lopp_pris = pris_for_lopp(prizes, min_pris)

        ban_value = await _extract_banforhallande_value_from_section(info_section)  
        underlag_for_lopp = sanitize_underlag(ban_value)

        rows = await grid.locator("div[role='row'][data-rowindex]").all()  
        if not rows:
            continue

        for row in rows:
            policy.check()
            def cell(field, current_row=row):
                return current_row.locator(f"div[data-field='{field}']")

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
                kusk_raw = await drv.evaluate(
                    """
                    (el) => {
                      const links = Array.from(el.querySelectorAll("a"));
                      if (!links.length) return (el.textContent || "").trim();

                      const active = links.filter(a => !a.closest("[class*='linethrough']"));
                      const pick = (active.length ? active : links)[(active.length ? active : links).length - 1];

                      return (pick.textContent || "").trim();
                    }
                    """
                )
                kusk = normalize_kusk(kusk_raw)
            except PlaywrightError as exc:
                logger.warning("Could not read driver for race %s, horse %s: %s", lopp, nr, exc)
                kusk = ""

            placetxt = normalize_cell_text(await cell("placementDisplay").inner_text())
            placering = map_placering_value(placetxt)

            dist_raw = normalize_cell_text(await cell("startPositionAndDistance").inner_text())
            distans, spar, _ = parse_dist_spar(dist_raw)

            tid_raw = normalize_cell_text(await cell("time").inner_text())
            tid, startmetod, galopp = parse_tid_cell(tid_raw)

            odds = None
            try:
                odds_cell = cell("odds")
                if await odds_cell.count() > 0:
                    odds_txt = normalize_cell_text(await odds_cell.inner_text())
                    mm = re.search(r"\d+", odds_txt)
                    if mm:
                        odds = int(mm.group(0))
            except (PlaywrightError, ValueError) as exc:
                logger.warning("Could not read odds for race %s, horse %s: %s", lopp, nr, exc)
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
                underlag=underlag_for_lopp,
                kusk=kusk,
                pris=lopp_pris,
                odds=odds,
            ))

    await policy.check_page(page)
    return data


@transaction.atomic
def write_rows_to_db(rows: list[Row]) -> int:
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
                defaults={
                    "nr": r.nr,
                    "distans": r.distans,
                    "spar": r.spar,
                    "placering": r.placering,
                    "tid": r.tid,
                    "startmetod": r.startmetod,
                    "galopp": r.galopp,
                    "underlag": (r.underlag or ""),
                    "kusk": normalize_kusk(r.kusk),
                    "pris": r.pris,
                    "odds": (r.odds if (r.odds not in (None, 999)) else 999),
                },
            )
        except IntegrityError:
            logger.exception("DB IntegrityError for (%s,%s,L%s,%s)", r.datum, r.bankod, r.lopp, namn_clean)
            raise

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

        incoming_startmetod = (r.startmetod or "").strip()
        if incoming_startmetod and obj.startmetod != incoming_startmetod:
            obj.startmetod = incoming_startmetod
            changed_fields.append("startmetod")

        if obj.galopp != (r.galopp or ""):
            obj.galopp = (r.galopp or "")
            changed_fields.append("galopp")

        incoming_underlag = (r.underlag or "").strip().lower()
        existing_underlag = (obj.underlag or "").strip().lower()
        if incoming_underlag != existing_underlag:
            obj.underlag = incoming_underlag
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
        if (
            incoming_odds not in (None, 999)
            and existing_odds == 999
            and obj.odds != int(incoming_odds)
        ):
            obj.odds = int(incoming_odds)
            changed_fields.append("odds")

        if changed_fields:
            obj.save(update_fields=changed_fields)
            updated_n += 1
        else:
            unchanged_n += 1

    logger.info("  db_created=%d db_updated=%d db_unchanged=%d", created_n, updated_n, unchanged_n)
    return created_n + updated_n


def _parse_ts_id(value: str) -> int:
    return int(str(value).replace("_", ""))


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}. Use YYYY-MM-DD.") from exc


def _format_ts_id(ts_id: int) -> str:
    s = str(ts_id)
    return f"{s[:-3]}_{s[-3:]}" if len(s) > 3 else s


def _results_ts_id_from_href(href: str) -> int | None:
    m = re.search(r"/race/raceday/ts(\d+)/results", href or "")
    return int(m.group(1)) if m else None


RESULTS_URL = "https://sportapp.travsport.se/race/raceday/ts{}/results/all"
RESULTS_CACHE_SECONDS = 24 * 60 * 60
CALENDAR_CACHE_SECONDS = 6 * 60 * 60


async def _calendar_results_for_month(page, policy: ScrapePolicy, month: date):
    """Discover real result links once per month, including lazy-loaded sections."""
    cache_key = f"results-calendar:{month.year}-{month.month:02d}"
    cached = policy.cached(cache_key, CALENDAR_CACHE_SECONDS)
    if cached is not None:
        logger.info("Using cached Resultat calendar for %s", month.strftime("%Y-%m"))
        return cached

    calendar_url = CALENDAR_URL.format(year=month.year, month=month.month)
    month_name = SWEDISH_MONTH_BY_NUMBER[month.month]
    results = {}
    complete = False
    try:
        logger.info("Opening Resultat calendar URL: %s", calendar_url)
        await policy.goto(page, calendar_url)
        await policy.wait_for_selector(page, "h2", timeout=120_000)
        await policy.wait_for_selector(
            page, "a[href*='/race/raceday/ts'][href*='/results']", timeout=120_000
        )
        previous_sample = None
        stable_bottom = 0

        for _ in range(40):
            await policy.check_page(page)
            snapshot = await page.evaluate(
                r"""
                (monthName) => {
                    const normalize = (value) => (value || "")
                        .normalize("NFKD")
                        .replace(/\s+/g, " ")
                        .trim()
                        .toLowerCase();
                    const datePattern = new RegExp(`(?:^|\\D)(\\d{1,2})\\s+${monthName}(?:\\b|$)`, "i");
                    const headers = Array.from(document.querySelectorAll("h2"));
                    const links = Array.from(document.querySelectorAll("a[href*='/race/raceday/ts'][href*='/results']"));
                    const before = (left, right) => Boolean(left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING);
                    const days = {};
                    for (let i = 0; i < headers.length; i++) {
                        const match = datePattern.exec(normalize(headers[i].textContent));
                        if (!match) continue;
                        const day = String(Number(match[1]));
                        if (!days[day]) days[day] = [];
                        for (const link of links) {
                            if (before(headers[i], link) && (!headers[i + 1] || before(link, headers[i + 1]))) {
                                days[day].push(link.getAttribute("href"));
                            }
                        }
                    }
                    const root = document.scrollingElement || document.documentElement;
                    return {
                        days,
                        top: root.scrollTop,
                        height: root.scrollHeight,
                        viewport: root.clientHeight,
                        atBottom: root.clientHeight > 0 && root.scrollTop + root.clientHeight >= root.scrollHeight - 8,
                    };
                }
                """,
                month_name,
            )
            for day, hrefs in snapshot["days"].items():
                ids = results.setdefault(day, [])
                for href in hrefs:
                    ts_id = _results_ts_id_from_href(urljoin("https://sportapp.travsport.se", href))
                    if ts_id is not None and ts_id not in ids:
                        ids.append(ts_id)

            loading = await page.locator(
                ".MuiDataGrid-loadingOverlay:visible, [role='progressbar']:visible, [aria-busy='true']:visible"
            ).count()
            sample = (
                snapshot["top"], snapshot["height"], snapshot["viewport"],
                tuple((day, tuple(ids)) for day, ids in sorted(results.items())),
            )
            if snapshot["atBottom"] and not loading and sample == previous_sample:
                stable_bottom += 1
                if stable_bottom >= 3:
                    complete = True
                    break
            else:
                stable_bottom = 0
            previous_sample = sample
            if not snapshot["atBottom"]:
                await page.evaluate("window.scrollBy(0, Math.max(1, window.innerHeight * 0.8))")
            # Request pacing can leave updates queued after a scroll; one
            # unchanged sample is not enough to treat discovery as complete.
            await asyncio.sleep(max(5, policy.request_max) + random.uniform(1, 3))

        await policy.check_page(page)
    except ScrapeStopped:
        raise
    except PlaywrightError as exc:
        await policy.failure(page, calendar_url, f"Resultat calendar failed: {exc}")
        return {}

    if not complete:
        await policy.failure(page, calendar_url, "Resultat calendar did not settle at the bottom; partial discovery was not cached")
        return {}

    if not any(results.values()):
        logger.warning("No usable Resultat calendar links found for %s.", month.strftime("%Y-%m"))
        await policy.failure(page, calendar_url, "Resultat calendar contained no usable result links")
        return {}

    policy.remember(cache_key, results)
    policy.success(calendar_url)
    return results


async def _discover_result_ids(page, policy: ScrapePolicy, first_day: date, last_day: date):
    ids = []
    month = first_day.replace(day=1)
    while month <= last_day:
        policy.check()
        month_results = await _calendar_results_for_month(page, policy, month)
        for day in sorted(month_results, key=int):
            result_day = month.replace(day=int(day))
            if first_day <= result_day <= last_day:
                for ts_id in month_results[day]:
                    if ts_id not in ids:
                        ids.append(ts_id)
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return ids


async def run_results(policy: ScrapePolicy, manual_range, first_day, last_day, ids_after_start) -> int:
    total_scraped = 0
    async with policy.browser() as page:
        if manual_range is not None:
            start_id, end_id = manual_range
            ts_ids = range(start_id, end_id + 1)
        else:
            ts_ids = await _discover_result_ids(page, policy, first_day, last_day)
            if not ts_ids:
                logger.info("No Resultat links found between %s and %s.", first_day, last_day)
                return 0
            if ids_after_start is not None:
                start_id = ts_ids[0]
                ts_ids = range(start_id, start_id + ids_after_start + 1)

        for ts_id in ts_ids:
            policy.check()
            url = RESULTS_URL.format(ts_id)
            if policy.is_recent(url, RESULTS_CACHE_SECONDS):
                logger.info("Skipping %s; successfully processed within the last 24 hours.", url)
                continue
            logger.info("Scraping %s", url)
            try:
                rows = await scrape_page(page, url, policy)
            except ScrapeStopped:
                raise
            except (PlaywrightError, ValueError) as exc:
                await policy.failure(page, url, f"Result parsing failed: {exc}")
                continue
            if not rows:
                await policy.failure(page, url, "Result page contained no usable rows")
                continue

            await asyncio.to_thread(write_rows_to_db, rows)
            policy.check()
            policy.success(url)
            total_scraped += len(rows)

    return total_scraped


class Command(BaseCommand):
    help = "Scrape actual Resultat calendar links from 5 days back through today, or explicit manual IDs."

    START_ID = 616_260
    END_ID = 616_310

    def add_arguments(self, parser):
        parser.add_argument(
            "--manual-ids",
            action="store_true",
            help="Use the hard-coded START_ID and END_ID values in this command.",
        )
        parser.add_argument(
            "--start-id",
            type=_parse_ts_id,
            help="Manual raceday ts-ID; only this ID is fetched unless a range is explicitly supplied.",
        )
        parser.add_argument(
            "--end-id",
            type=_parse_ts_id,
            help="Manual last raceday ts-ID, for example 616_310. Requires --start-id.",
        )
        parser.add_argument(
            "--ids-after-start",
            type=int,
            default=None,
            help="Explicitly add this many sequential IDs after --start-id or the first link on the calendar date. The default follows actual calendar links.",
        )
        parser.add_argument(
            "--days-back",
            type=int,
            default=5,
            help="Include calendar results from this many days ago through today (default: 5).",
        )
        parser.add_argument(
            "--calendar-date",
            type=_parse_iso_date,
            help="Scrape actual Resultat links for this date only. Use YYYY-MM-DD.",
        )

    def handle(self, *args, **opts):
        ids_after_start = opts.get("ids_after_start")
        if ids_after_start is not None and ids_after_start < 0:
            raise CommandError("--ids-after-start must be 0 or greater.")
        days_back = opts["days_back"]
        if days_back < 0:
            raise CommandError("--days-back must be 0 or greater.")

        start_id = opts.get("start_id")
        end_id = opts.get("end_id")
        manual_range = None
        if start_id is not None:
            end_id = end_id if end_id is not None else start_id + (ids_after_start or 0)
            if start_id <= 0 or end_id < start_id:
                raise CommandError("IDs must be positive and --end-id must be greater than or equal to --start-id.")
            manual_range = (start_id, end_id)
        elif end_id is not None:
            raise CommandError("--end-id requires --start-id.")
        elif opts.get("manual_ids"):
            if self.START_ID <= 0 or self.END_ID < self.START_ID:
                raise CommandError("START_ID must be positive and END_ID must be greater than or equal to START_ID.")
            manual_range = (self.START_ID, self.END_ID)

        today = timezone.localdate(timezone=ZoneInfo("Europe/Stockholm"))
        first_day = opts.get("calendar_date") or (today - timedelta(days=days_back))
        last_day = first_day if opts.get("calendar_date") or ids_after_start is not None else today
        if manual_range is not None:
            logger.info("Using manual range: ts%s through ts%s", *map(_format_ts_id, manual_range))
        else:
            logger.info("Finding Resultat links from %s through %s.", first_day, last_day)

        with ScrapePolicy() as policy:
            total = asyncio.run(run_results(policy, manual_range, first_day, last_day, ids_after_start))
        self.stdout.write(self.style.SUCCESS(f"Done. {total} rows scraped & processed."))
