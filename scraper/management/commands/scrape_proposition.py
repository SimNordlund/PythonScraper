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
    name_up = name.strip().upper()
    if name_up.startswith(("TÄVLINGSDAG", "TRAVTÄVLING")):
        name_up = name_up.split(maxsplit=1)[1]
    if name_up in FULLNAME_TO_BANKOD:
        return FULLNAME_TO_BANKOD[name_up]
    return _ASCII_FALLBACK.get(_strip_diacritics(name_up), name_up[:2].title())

def extract_bankod_from_text(raw: str) -> str | None:
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
    distans: int | None = None          # //Changed!
    kuskanskemal: str | None = None     # //Changed!

# --------------------------------------
#  A) Skrapa en enskild proposition-sida
# --------------------------------------
async def scrape_proposition_page(url: str) -> List[PropRow]:
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

        # Bana + datum
        bankod = None; startdatum = None
        nav = page.locator("div[class*='RaceDayNavigator_title'] span")
        if await nav.count() >= 2:
            track_text = (await nav.nth(0).inner_text()).strip()
            bank_try = extract_bankod_from_text(track_text) or track_to_bankod(track_text)
            date_text = (await nav.nth(1).inner_text()).strip()
            bankod = bank_try
            startdatum = int(swedish_date_to_yyyymmdd(date_text))
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
                break

        if bankod is None or startdatum is None:
            await browser.close(); return []

        # Propositionnummer (“Prop. X”)
        prop_num = None
        cand = page.locator("xpath=//*[contains(normalize-space(.), 'Prop.')]")
        for i in range(min(await cand.count(), 50)):
            txt = (await cand.nth(i).inner_text()).strip()
            m = re.search(r"Prop\.\s*(\d+)", txt, flags=re.I)
            if m:
                prop_num = int(m.group(1))
                break
        if prop_num is None:
            await browser.close(); return []

        # Hämta "Ligan" = kuskanskemal (en gång per sida)                # //Changed!
        kuskanskemal: str | None = None                                  # //Changed!
        try:                                                              # //Changed!
            lig = page.locator("xpath=(//*[contains(., 'Ligan')])[1]")    # //Changed!
            if await lig.count() > 0:                                     # //Changed!
                t1 = (await lig.first.inner_text()).strip()               # //Changed!
                # Kolla om värdet står i nästa syskon                      # //Changed!
                nxt = lig.first.locator("xpath=following-sibling::*[1]")  # //Changed!
                t2 = (await nxt.first.inner_text()).strip() if await nxt.count() else ""  # //Changed!
                blob = f"{t1} {t2}".strip()                               # //Changed!
                m = re.search(r"Ligan\s*[:\-]?\s*(.+)", blob, flags=re.I) # //Changed!
                if m:                                                     # //Changed!
                    kuskanskemal = m.group(1).strip()                     # //Changed!
        except Exception:                                                 # //Changed!
            pass                                                          # //Changed!

        # Hästnamn + distans per rad
        rows = await page.locator("div[role='row'][data-rowindex]").all()
        out: List[PropRow] = []
        for row in rows:
            # namn
            cell = row.locator("div[data-field='horseName'], div[data-field='horse']")
            if await cell.count() == 0:
                continue
            name_loc = cell.locator("a, span").first
            namn_raw = (await name_loc.inner_text()).strip()
            namn = namn_raw.split("(")[0].strip()
            if not namn:
                continue

            # distans (kan saknas)                                         # //Changed!
            dist_val: int | None = None                                    # //Changed!
            dist_cell = row.locator("div[data-field='distance']")          # //Changed!
            if await dist_cell.count() > 0:                                # //Changed!
                dist_txt = (await dist_cell.first.inner_text()).strip()    # //Changed!
                m = re.search(r"(\d{3,5})", dist_txt)                      # //Changed!
                if m:                                                      # //Changed!
                    dist_val = int(m.group(1))                             # //Changed!

            out.append(PropRow(
                startdatum, bankod, namn, prop_num,
                dist_val,                                                 # //Changed!
                kuskanskemal,                                             # //Changed!
            ))

        await browser.close()
        return out

#  B) Hämta alla proposition-IDs från dag-listan (grid-sida)
async def fetch_prop_ids_for_day(day_id: int) -> List[int]:
    list_url = f"https://sportapp.travsport.se/propositions/raceday/ts{day_id}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(); ctx.set_default_timeout(120_000)
        page = await ctx.new_page()
        try:
            await page.goto(list_url, timeout=0)
        except PlaywrightError:
            await browser.close(); return []

        link_sel = f"a[href*='/propositions/raceday/ts{day_id}/proposition/ts']"
        try:
            await page.wait_for_selector(link_sel, timeout=10_000)
        except PlaywrightError:
            await browser.close(); return []

        scroller = page.locator("div.MuiDataGrid-virtualScroller, div[class*='MuiDataGrid-virtualScroller']")
        last = -1
        for _ in range(25):
            count = await page.locator(link_sel).count()
            if count == last:
                break
            last = count
            try:
                if await scroller.count() > 0:
                    await scroller.first.evaluate("(el)=>el.scrollTo(0, el.scrollHeight)")
                else:
                    await page.mouse.wheel(0, 20000)
            except Exception:
                pass
            await page.wait_for_timeout(300)

        hrefs = []
        links = page.locator(link_sel)
        for i in range(await links.count()):
            href = await links.nth(i).get_attribute("href")
            if href:
                hrefs.append(href)
        await browser.close()

    ids = set()
    for h in hrefs:
        m = re.search(r"/proposition/ts(\d+)", h)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)

#  Management command
class Command(BaseCommand):
    help = "Scrape proposition-sidor: loopa över raceday-id, hämta prop-ids från list-sidan och skrapa dem."

    DAY_START_ID = 610_132
    DAY_END_ID   = 610_300

    def handle(self, *args, **opts):
        base_prop = "https://sportapp.travsport.se/propositions/raceday/ts{}/proposition/ts{}"
        grand_total = 0

        for day_id in range(self.DAY_START_ID, self.DAY_END_ID + 1):
            logging.info("=== Raceday ts%d: hämtar proposition-länkar ===", day_id)
            try:
                prop_ids = asyncio.run(fetch_prop_ids_for_day(day_id))
            except Exception as exc:
                logging.warning("  kunde inte hämta prop-ids för ts%d: %s", day_id, exc)
                prop_ids = []

            if not prop_ids:
                logging.info("  inga proposition-länkar hittade för ts%d", day_id)
                continue

            day_total = 0
            for pid in prop_ids:
                url = base_prop.format(day_id, pid)
                logging.info("  Scraping %s", url)
                try:
                    rows = asyncio.run(scrape_proposition_page(url))
                except Exception as exc:
                    logging.warning("    failed: %s", exc)
                    rows = []

                if not rows:
                    logging.info("    no rows")
                    continue

                for r in rows:
                    Proposition.objects.update_or_create(
                        startdatum=r.startdatum, bankod=r.bankod,
                        namn=r.namn, proposition=r.proposition,
                        defaults={
                            "distans": r.distans,              # //Changed!
                            "kuskanskemal": r.kuskanskemal,    # //Changed!
                        },
                    )
                cnt = len(rows)
                day_total += cnt
                grand_total += cnt
                logging.info("    inserted/updated %d rows", cnt)

            logging.info("=== Klar dag ts%d: %d rader ===", day_id, day_total)

        self.stdout.write(self.style.SUCCESS(f"Done. {grand_total} rows processed."))
