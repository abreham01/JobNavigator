"""
Ethiopian Job Scraper — Unified Module
========================================
Sites: HahuJobs | EthioJobs | PalmJobs | NGOJobs Ethiopia | GeezJobs
Filter: Accounting & Finance roles only
Step 1: Scrape all 5 sites → print results to terminal
Step 2: Push to Neon PostgreSQL (run with --push flag)
Step 3: Every-6-hour scheduler (run with --schedule flag)

INSTALL:
  pip install requests beautifulsoup4 psycopg2-binary python-dotenv
  pip install playwright && playwright install chromium   <- for HahuJobs AND EthioJobs (both are React SPAs)
  pip install curl-cffi                                   <- for GeezJobs

USAGE:
  python ethiopian_job_scraper.py              # scrape + print only
  python ethiopian_job_scraper.py --push       # scrape + push to Neon DB
  python ethiopian_job_scraper.py --schedule   # scrape every 6h + push
  python ethiopian_job_scraper.py --site geez  # test one site only
"""

from __future__ import annotations
import re, time, logging, argparse, asyncio, os, json
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ethiopian_scraper")

# ---------------------------------------------------------------------------
# FILTER KEYWORDS — Accounting & Finance roles
# ---------------------------------------------------------------------------

FINANCE_KEYWORDS = [
    "finance", "financial", "accounting", "accountant", "accounts",
    "audit", "auditor", "budget", "treasury",
    "bookkeeping", "bookkeeper", "payroll", "tax", "taxation",
    "banking", "teller", "microfinance",
]

def _is_finance_job(job: dict) -> bool:
    haystack = " ".join([
        job.get("title", ""),
        job.get("description", ""),
        job.get("category", ""),
    ]).lower()
    return any(kw.lower() in haystack for kw in FINANCE_KEYWORDS)


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def _safe_str(v, fallback="") -> str:
    """Safely coerce any value to str; module-level so all scrapers can use it."""
    if v is None or v == "":
        return fallback
    if isinstance(v, (list, dict)):
        return fallback
    if isinstance(v, (int, float)):
        return str(v)
    return str(v)


# ===========================================================================
# SITE 1 — HahuJobs (hahu.jobs)
# React SPA — Playwright intercepts network JSON responses
# ===========================================================================

async def _scrape_hahujobs_async() -> list[dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "HahuJobs: playwright not installed. "
            "Run: pip install playwright && playwright install chromium"
        )
        return []

    logger.info("HahuJobs: launching Playwright (networkidle wait)...")
    captured_jobs: list[dict] = []
    intercepted_urls: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = await context.new_page()

        async def handle_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if response.status == 200 and "application/json" in ct:
                    intercepted_urls.append(response.url)
                    data = await response.json()
                    before = len(captured_jobs)
                    _extract_hahu_jobs(data, captured_jobs)
                    added = len(captured_jobs) - before
                    if added:
                        logger.debug("HahuJobs: +%d jobs from %s", added, response.url)
            except Exception as exc:
                logger.debug("HahuJobs: response parse error (%s): %s", response.url, exc)

        page.on("response", handle_response)

        target_url = "https://www.hahu.jobs/jobs"
        try:
            logger.info("HahuJobs: navigating to %s", target_url)
            await page.goto(target_url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            logger.warning("HahuJobs: networkidle timed out (%s) — proceeding anyway", e)

        await page.wait_for_timeout(3000)

        for i in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2500)
            logger.debug("HahuJobs: scroll %d, captured so far: %d", i + 1, len(captured_jobs))

        await browser.close()

    logger.info(
        "HahuJobs: intercepted %d JSON responses, raw job records: %d",
        len(intercepted_urls), len(captured_jobs),
    )

    jobs = []
    seen: set[str] = set()
    for item in captured_jobs:
        if not isinstance(item, dict):
            continue

        title = (
            item.get("title") or item.get("position") or item.get("job_title")
            or item.get("vacancy_title") or item.get("name")
        )
        job_id = str(
            item.get("_id") or item.get("id") or item.get("uid")
            or item.get("uuid") or item.get("vacancy_id") or ""
        )
        if not title or not job_id or job_id in seen:
            continue
        seen.add(job_id)

        company = "Unknown"
        for key in ("entity", "company", "employer", "organization", "org"):
            val = item.get(key)
            if isinstance(val, dict):
                company = (val.get("name") or val.get("title") or val.get("company_name") or company)
                break
            elif isinstance(val, str) and val.strip():
                company = val.strip()
                break

        loc = item.get("location") or item.get("city") or item.get("region") or "Addis Ababa"
        if isinstance(loc, dict):
            loc = loc.get("name") or loc.get("city") or "Addis Ababa"

        jtype = item.get("type") or item.get("job_type") or item.get("employment_type") or "Full-time"
        if isinstance(jtype, dict):
            jtype = jtype.get("name") or "Full-time"

        deadline = item.get("deadline") or item.get("closing_date") or item.get("expiry_date") or "N/A"

        jobs.append({
            "job_id":      f"hahu_{job_id}",
            "source":      "HahuJobs",
            "title":       _clean(str(title)),
            "company":     _clean(str(company)),
            "location":    _clean(str(loc)),
            "job_type":    _clean(str(jtype)),
            "deadline":    str(deadline),
            "posted_date": item.get("created_at") or item.get("posted_date") or item.get("createdAt"),
            "description": _clean(str(item.get("description") or item.get("summary") or item.get("detail") or "")),
            "salary_text": str(item.get("salary") or "") or None,
            "salary_min":  item.get("salary_min") or item.get("min_salary"),
            "salary_max":  item.get("salary_max") or item.get("max_salary"),
            "url":         f"https://www.hahu.jobs/jobs/{job_id}",
            "is_remote":   bool(item.get("is_remote", False)),
            "apply_url":   None,
            "experience":  item.get("experience") or item.get("experience_level"),
            "category":    _clean(str(item.get("sector") or item.get("category") or item.get("field") or "N/A")),
        })

    logger.info("HahuJobs: %d normalised jobs", len(jobs))
    return jobs


def _extract_hahu_jobs(data, out: list):
    JOB_KEYS = {
        "title", "position", "job_title", "vacancy", "vacancy_title",
        "_id", "closing_date", "deadline", "expiry_date",
    }

    def _looks_like_jobs(lst: list) -> bool:
        sample = [x for x in lst[:5] if isinstance(x, dict)]
        if not sample:
            return False
        for item in sample:
            keys = {str(k).lower() for k in item.keys()}
            if keys & JOB_KEYS:
                return True
        return False

    if isinstance(data, list):
        if _looks_like_jobs(data):
            out.extend(data)
            return
        for item in data:
            _extract_hahu_jobs(item, out)
    elif isinstance(data, dict):
        for val in data.values():
            if isinstance(val, (list, dict)):
                _extract_hahu_jobs(val, out)


def scrape_hahujobs() -> list[dict]:
    return asyncio.run(_scrape_hahujobs_async())


# ===========================================================================
# SITE 2 — EthioJobs (ethiojobs.net)
# Strategy: extract from __NEXT_DATA__ JSON embedded in the page HTML.
# EthioJobs is Next.js with SSR — the full jobs payload is serialised into
# a <script id="__NEXT_DATA__"> tag on every page load, so a plain HTTP GET
# is all we need (no Playwright, no JS execution).
#
# Pagination: iterate /jobs?page=N until no new jobs appear.
# Fallback: if __NEXT_DATA__ is absent (future site change), try Playwright.
# ===========================================================================

# Field-name aliases in priority order (first non-empty value wins)
_ETHIO_TITLE_KEYS = ("title", "jobTitle", "job_title", "position", "name")

_ETHIO_COMPANY_KEYS = (
    "company", "companyName", "company_name", "employer", "employerName",
    "organization", "organisation", "orgName", "org_name", "client",
)

_ETHIO_LOCATION_KEYS = (
    "location", "locations", "city", "cities", "region", "regions",
    "workLocation", "work_location", "jobLocation", "job_location",
    "place", "address",
)

_ETHIO_DEADLINE_KEYS = (
    "deadline", "closingDate", "closing_date", "expiryDate", "expiry_date",
    "applicationDeadline", "application_deadline", "closeDate", "close_date",
    "dueDate", "due_date", "endDate", "end_date", "validUntil", "valid_until",
)

_ETHIO_POSTED_KEYS = (
    "createdAt", "created_at", "postedDate", "posted_date",
    "publishedAt", "published_at", "datePosted", "date_posted",
    "postDate", "post_date",
)

_ETHIO_TYPE_KEYS = (
    "jobType", "job_type", "type", "employmentType", "employment_type",
    "contractType", "contract_type", "workType", "work_type",
)

_ETHIO_SALARY_KEYS = (
    "salary", "salaryRange", "salary_range", "compensation",
    "salaryMin", "salary_min", "minSalary", "min_salary",
)

_ETHIO_SLUG_KEYS = ("slug", "id", "_id", "uid", "uuid", "jobId", "job_id")

_ETHIO_DESC_KEYS = (
    "description", "jobDescription", "job_description",
    "summary", "overview", "detail", "details", "body",
)

_ETHIO_CAT_KEYS = (
    "category", "categories", "sector", "field", "industry",
    "jobCategory", "job_category", "fieldOfWork", "field_of_work",
)

_ETHIO_EXP_KEYS = (
    "experience", "experienceLevel", "experience_level",
    "yearsOfExperience", "years_of_experience", "requiredExperience",
)


def _ethio_first(item: dict, keys: tuple, fallback=None):
    """
    Return the first non-empty value found in `item` for the given key list.
    Safely unwraps nested dicts (tries name/title/value/label/text sub-keys)
    and single-element lists.
    """
    for k in keys:
        v = item.get(k)
        if v is None or v == "" or v == []:
            continue
        # Unwrap single-item list
        if isinstance(v, list):
            v = v[0] if v else None
            if v is None:
                continue
        # Unwrap nested object
        if isinstance(v, dict):
            for sub in ("name", "title", "value", "label", "text"):
                sv = v.get(sub)
                if sv and isinstance(sv, str) and sv.strip():
                    return sv.strip()
            continue
        if isinstance(v, (int, float)):
            return str(v)
        val = str(v).strip()
        if val:
            return val
    return fallback


def _ethio_company(item: dict) -> str:
    """
    Dedicated company resolver — handles flat string, nested dict, and
    list-of-objects, across all known key aliases.
    """
    for ck in _ETHIO_COMPANY_KEYS:
        raw = item.get(ck)
        if raw is None or raw == "":
            continue
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if isinstance(raw, dict):
            for sub in (
                "name", "title", "companyName", "company_name",
                "legalName", "legal_name", "displayName", "display_name",
            ):
                sv = raw.get(sub)
                if sv and isinstance(sv, str) and sv.strip():
                    return sv.strip()
        elif isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "Unknown"


def _ethio_location(item: dict) -> str:
    """
    Location resolver — handles nested objects, list-of-locations, plain strings.
    """
    for lk in _ETHIO_LOCATION_KEYS:
        raw = item.get(lk)
        if raw is None or raw == "" or raw == []:
            continue
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if isinstance(raw, dict):
            for sub in ("name", "city", "region", "title", "label"):
                sv = raw.get(sub)
                if sv and isinstance(sv, str) and sv.strip():
                    return sv.strip()
        elif isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "Ethiopia"


def scrape_ethiojobs() -> list[dict]:
    """
    Primary strategy: fetch each /jobs?page=N page and extract the job list
    from the <script id="__NEXT_DATA__"> JSON blob that Next.js embeds in
    every server-rendered page.  No Playwright needed.

    Fallback: if __NEXT_DATA__ is missing on the first page (future site
    change), automatically retries with a Playwright headless browser.
    """
    logger.info("EthioJobs: fetching via __NEXT_DATA__ (SSR)...")

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    BASE     = "https://ethiojobs.net"
    MAX_PAGES = 5

    raw_items: list[dict] = []
    seen_ids:  set[str]   = set()
    used_next_data        = False

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE}/jobs" + (f"?page={page_num}" if page_num > 1 else "")
        try:
            # Try httpx first (better HTTP/2 support); fall back to requests
            try:
                import httpx
                with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                    resp = client.get(url, headers=HEADERS)
                html = resp.text
            except ImportError:
                r    = requests.get(url, headers=HEADERS, timeout=20)
                r.raise_for_status()
                html = r.text
        except Exception as e:
            logger.warning("EthioJobs: GET failed %s: %s", url, e)
            break

        soup   = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")

        if not script:
            if page_num == 1:
                logger.warning(
                    "EthioJobs: __NEXT_DATA__ not found on page 1 — "
                    "site may have changed, trying Playwright fallback..."
                )
                return _scrape_ethiojobs_playwright_fallback()
            # Later pages without __NEXT_DATA__ means we've gone past the end
            logger.info("EthioJobs: no __NEXT_DATA__ on page %d — stopping", page_num)
            break

        used_next_data = True

        try:
            json_data  = json.loads(script.string)
        except Exception as e:
            logger.warning("EthioJobs: JSON parse failed on page %d: %s", page_num, e)
            break

        page_props = json_data.get("props", {}).get("pageProps", {})

        # Walk known pageProps keys where the jobs array may live
        raw = (
            page_props.get("jobs")
            or page_props.get("initialJobs")
            or page_props.get("jobsData")
            or page_props.get("fallbackData")
            or {}
        )

        # Unwrap common pagination wrappers: {results:[...]}, {data:[...]}, etc.
        items: list = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = (
                raw.get("results")
                or raw.get("data")
                or raw.get("jobs")
                or raw.get("items")
                or []
            )
            # Last-resort: first list value in the dict
            if not items:
                for v in raw.values():
                    if isinstance(v, list) and v:
                        items = v
                        break

        if not isinstance(items, list) or not items:
            logger.info("EthioJobs: no items found on page %d — stopping", page_num)
            break

        new_on_page = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            # Deduplicate by raw id/slug before full normalisation
            raw_id = str(
                item.get("id") or item.get("_id") or item.get("slug")
                or item.get("uid") or ""
            )
            if raw_id and raw_id in seen_ids:
                continue
            if raw_id:
                seen_ids.add(raw_id)
            raw_items.append(item)
            new_on_page += 1

        logger.info("EthioJobs: page %d — %d new raw items (total: %d)",
                    page_num, new_on_page, len(raw_items))

        # If we got nothing new this page, stop paginating
        if new_on_page == 0:
            break

        time.sleep(0.8)

    if used_next_data:
        logger.info("EthioJobs: __NEXT_DATA__ extraction complete — %d raw items", len(raw_items))
    if not raw_items:
        logger.warning("EthioJobs: zero items extracted")
        return []

    return _normalise_ethiojobs(raw_items)


def _normalise_ethiojobs(raw_items: list[dict]) -> list[dict]:
    """Convert raw API/NEXT_DATA dicts into the standard job schema."""
    jobs: list[dict] = []
    seen: set[str]   = set()

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        title = _ethio_first(item, _ETHIO_TITLE_KEYS)
        if not title:
            continue

        slug = _ethio_first(item, _ETHIO_SLUG_KEYS)
        if not slug:
            continue
        job_id = f"ethio_{slug}"
        if job_id in seen:
            continue
        seen.add(job_id)

        url = (
            item.get("url") or item.get("link")
            or item.get("jobUrl") or item.get("job_url")
            or f"https://ethiojobs.net/job/{slug}"
        )

        is_remote = bool(
            item.get("isRemote") or item.get("is_remote") or item.get("remote")
            or item.get("workFromHome") or item.get("work_from_home")
        )

        apply_url = (
            item.get("applyUrl") or item.get("apply_url")
            or item.get("applicationUrl") or item.get("application_url")
            or item.get("applyLink") or item.get("apply_link")
        )

        jobs.append({
            "job_id":      job_id,
            "source":      "EthioJobs",
            "title":       _clean(title),
            "company":     _clean(_ethio_company(item)),
            "location":    _clean(_ethio_location(item)),
            "job_type":    _clean(_ethio_first(item, _ETHIO_TYPE_KEYS, "Full-time")),
            "deadline":    _safe_str(_ethio_first(item, _ETHIO_DEADLINE_KEYS), "N/A"),
            "posted_date": _safe_str(_ethio_first(item, _ETHIO_POSTED_KEYS)) or None,
            "description": _clean(_safe_str(_ethio_first(item, _ETHIO_DESC_KEYS), "")),
            "salary_text": _safe_str(_ethio_first(item, _ETHIO_SALARY_KEYS)) or None,
            "salary_min":  (item.get("salaryMin") or item.get("salary_min")
                            or item.get("minSalary") or item.get("min_salary")),
            "salary_max":  (item.get("salaryMax") or item.get("salary_max")
                            or item.get("maxSalary") or item.get("max_salary")),
            "url":         url,
            "is_remote":   is_remote,
            "apply_url":   apply_url,
            "experience":  _safe_str(_ethio_first(item, _ETHIO_EXP_KEYS)) or None,
            "category":    _clean(_safe_str(_ethio_first(item, _ETHIO_CAT_KEYS), "N/A")),
        })

    logger.info("EthioJobs: %d jobs after normalise", len(jobs))
    return jobs


def _scrape_ethiojobs_playwright_fallback() -> list[dict]:
    """
    Playwright-based fallback for EthioJobs — only used if __NEXT_DATA__ is
    absent (i.e. the site has moved away from Next.js SSR).
    Intercepts XHR/fetch JSON responses emitted by the React app.
    """
    try:
        return asyncio.run(_scrape_ethiojobs_playwright_async())
    except Exception as e:
        logger.error("EthioJobs Playwright fallback crashed: %s", e)
        return []


async def _scrape_ethiojobs_playwright_async() -> list[dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "EthioJobs: playwright not installed — cannot use fallback. "
            "Run: pip install playwright && playwright install chromium"
        )
        return []

    logger.info("EthioJobs: Playwright fallback — intercepting XHR/fetch JSON...")
    captured_raw: list[dict] = []
    intercepted_urls: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = await context.new_page()

        async def handle_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if response.status == 200 and "application/json" in ct:
                    intercepted_urls.append(response.url)
                    data = await response.json()
                    before = len(captured_raw)
                    # Re-use the same recursive extractor logic inline
                    _pw_extract(data, captured_raw)
                    added = len(captured_raw) - before
                    if added:
                        logger.debug("EthioJobs PW: +%d from %s", added, response.url)
            except Exception as exc:
                logger.debug("EthioJobs PW: parse error (%s): %s", response.url, exc)

        page.on("response", handle_response)

        try:
            await page.goto("https://ethiojobs.net/jobs", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning("EthioJobs PW: navigation error: %s", e)

        await page.wait_for_timeout(4000)

        for i in range(4):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                break
            await page.wait_for_timeout(2000)

        await browser.close()

    logger.info("EthioJobs PW: %d intercepted URLs, %d raw records",
                len(intercepted_urls), len(captured_raw))
    return _normalise_ethiojobs(captured_raw)


def _pw_extract(data, out: list):
    """Recursively collect job-like dicts from any JSON shape (Playwright fallback)."""
    JOB_KEYS = {
        "id", "_id", "slug", "title", "jobTitle", "job_title", "position",
        "deadline", "closingDate", "closing_date", "company", "companyName",
    }
    JOB_KEYS_LOWER = {k.lower() for k in JOB_KEYS}

    def _looks_like_jobs(lst: list) -> bool:
        sample = [x for x in lst[:5] if isinstance(x, dict)]
        for item in sample:
            keys = {str(k).lower() for k in item.keys()}
            if keys & JOB_KEYS_LOWER:
                return True
        return False

    if isinstance(data, list):
        if _looks_like_jobs(data):
            out.extend(data)
            return
        for item in data:
            _pw_extract(item, out)
    elif isinstance(data, dict):
        for val in data.values():
            if isinstance(val, (list, dict)):
                _pw_extract(val, out)


# ===========================================================================
# SITE 3 — PalmJobs (palmjobs.et)
# ===========================================================================

_UUID_RE     = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_DATE_KW     = {"today","yesterday","ago","week","day","month","hour","minute","just now"}
_JOB_TYPE_KW = {"full-time","part-time","contract","internship","volunteer","remote","freelance","temporary"}
_PLACES      = ["addis ababa","addis","ethiopia","hawassa","dire dawa","mekelle","bahir dar","jimma","adama","gondar","gambela","remote"]

def _palm_is_date(t: str) -> bool: return any(k in t.lower() for k in _DATE_KW)
def _palm_is_type(t: str) -> bool: return t.lower() in _JOB_TYPE_KW
def _palm_is_loc(t: str) -> bool:  return any(p in t.lower() for p in _PLACES)
def _palm_is_exp(t: str) -> bool:  return bool(re.match(r"^\d[\d\-\+]* years?$", t, re.I))


def scrape_palmjobs() -> list[dict]:
    logger.info("PalmJobs: scraping SSR category pages...")
    SSR_PAGES = ["/jobs/addis-ababa", "/jobs/finance", "/jobs/ngo",
                 "/jobs/government", "/jobs/healthcare"]
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    BASE = "https://palmjobs.et"
    jobs = []
    seen = set()

    for path in SSR_PAGES:
        url = BASE + path
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
        except Exception as e:
            logger.warning("PalmJobs: GET failed %s: %s", url, e)
            continue

        soup  = BeautifulSoup(r.text, "html.parser")
        links = soup.find_all("a", href=_UUID_RE)

        for link in links:
            href = link.get("href", "")
            uuid = _UUID_RE.search(href)
            if not uuid:
                continue
            job_url = BASE + href if href.startswith("/") else href
            job_id  = f"palm_{uuid.group(0)}"
            if job_id in seen:
                continue
            seen.add(job_id)

            raw   = link.get_text(separator="\n", strip=True)
            lines = [_clean(l) for l in raw.splitlines() if _clean(l)]
            if lines and lines[-1].lower() == "view":
                lines.pop()
            if len(lines) < 3:
                continue

            company    = lines[0]
            posted     = None
            title      = None
            desc       = None
            location   = "Addis Ababa, Ethiopia"
            job_type   = "Full-Time"
            experience = None

            t_idx = 2 if (len(lines) > 1 and _palm_is_date(lines[1])) else 1
            if t_idx <= 1 and len(lines) > 1 and _palm_is_date(lines[1]):
                posted = lines[1]
            if t_idx < len(lines):
                title = lines[t_idx]

            for line in lines[t_idx + 1:]:
                if _palm_is_date(line):   posted   = line
                elif _palm_is_type(line): job_type  = line
                elif _palm_is_exp(line):  experience = line
                elif _palm_is_loc(line):  location   = line
                elif len(line) > 40:      desc       = line

            if not title or len(title) < 3:
                continue

            jobs.append({
                "job_id":      job_id,
                "source":      "PalmJobs",
                "title":       title.strip(),
                "company":     company.strip(),
                "location":    location.strip(),
                "job_type":    job_type,
                "deadline":    "N/A",
                "posted_date": posted,
                "description": (desc or "").strip(),
                "salary_text": None, "salary_min": None, "salary_max": None,
                "experience":  experience,
                "url":         job_url,
                "is_remote":   "remote" in location.lower(),
                "apply_url":   None,
                "category":    "N/A",
            })

        time.sleep(1)

    logger.info("PalmJobs: enriching %d jobs with detail pages...", len(jobs))
    for job in jobs:
        detail = _palm_detail(job["url"], HEADERS)
        for k, v in detail.items():
            if v:
                job[k] = v
        time.sleep(0.8)

    logger.info("PalmJobs: %d jobs total", len(jobs))
    return jobs


def _palm_detail(url: str, headers: dict) -> dict:
    try:
        r = requests.get(url, headers=headers or {}, timeout=15)
        r.raise_for_status()
    except Exception:
        return {}
    soup = BeautifulSoup(r.text, "html.parser")
    for noise in soup.find_all(["nav", "header", "footer", "script", "style"]):
        noise.decompose()
    text   = soup.get_text(separator="\n")
    result: dict = {}

    dl_m = re.search(
        r"Closes?\s+(?:in\s+\S+\s+\S+\s*[.|]\s*)?"
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4})",
        text, re.I,
    )
    if dl_m:
        result["deadline"] = dl_m.group(1).strip()

    sal_m = re.search(r"(\d[\d,\.]+\s*(?:birr|ETB))", text, re.I)
    if sal_m:
        result["salary_text"] = sal_m.group(1)

    h1 = soup.find("h1")
    if h1:
        parts = []
        for el in h1.find_all_next(["p", "li"]):
            t = _clean(el.get_text())
            if not t:
                continue
            if any(kw in t.lower() for kw in ["how to apply", "apply now", "similar"]):
                break
            parts.append(t)
        if parts:
            result["description"] = " ".join(parts)[:2000]

    return result


# ===========================================================================
# SITE 4 — NGOJobs Ethiopia (ngojobs.et)
# ===========================================================================

def scrape_ngojobs() -> list[dict]:
    logger.info("NGOJobs: scraping keyword search pages...")
    BASE     = "https://ngojobs.et"
    JOBS_URL = "https://ngojobs.et/jobs"
    KEYWORDS = ["finance", "accounting", "audit", "budget", "grant"]
    HEADERS  = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://ngojobs.et/",
    }
    MAX_PAGES = 3

    JOB_TYPE_RE = re.compile(r"^(Full-Time|Contract|Consultancy|Internship|Part-time|Volunteer)$", re.I)
    POSTED_RE   = re.compile(r"^Posted\s+(\d{1,2}/\d{1,2}/\d{4})$", re.I)
    DEADLINE_RE = re.compile(r"^Deadline[:\s]+(\d{1,2}/\d{1,2}/\d{4})$", re.I)
    LOCATION_RE = re.compile(r"Ethiopia|Addis Ababa|Hawassa|Dire Dawa|Afar|Amhara|Oromia|Tigray|Gambela|Somali|Harari", re.I)
    SKIP_RE     = re.compile(r"^(apply now|apply|view all|login|jobs|companies|ngojobs|get app)$", re.I)

    jobs: list[dict] = []
    seen: set[str]   = set()

    def _parse_card(link_tag) -> Optional[dict]:
        href = link_tag.get("href", "")
        if not href.startswith("/jobs/") or href in ("/jobs", "/jobs/"):
            return None
        slug    = href.replace("/jobs/", "").strip("/")
        job_url = BASE + href
        raw     = link_tag.get_text(separator="\n", strip=True)
        lines   = [_clean(l) for l in raw.splitlines() if _clean(l)]
        lines   = [l for l in lines if not SKIP_RE.match(l)]
        lines   = [l for l in lines if not re.match(r"^.{3,60}\s+logo$", l, re.I)]
        if len(lines) < 2:
            return None

        title = company = location = job_type = description = posted_date = deadline = None
        for line in lines:
            pm = POSTED_RE.match(line)
            dm = DEADLINE_RE.match(line)
            if pm:
                posted_date = pm.group(1)
            elif dm:
                deadline = dm.group(1)
            elif JOB_TYPE_RE.match(line):
                job_type = line
            elif LOCATION_RE.search(line) and len(line) < 100:
                location = line
            elif title is None and len(line) >= 4 and not LOCATION_RE.search(line):
                title = line
            elif (company is None and title and len(line) >= 3
                  and not LOCATION_RE.search(line) and not JOB_TYPE_RE.match(line)):
                company = line
            elif len(line) > 60 and description is None:
                description = line

        if not title:
            return None
        return {
            "job_id":      f"ngojobs_{slug}",
            "source":      "NGOJobs Ethiopia",
            "title":       title,
            "company":     (company or "Unknown").strip(),
            "location":    (location or "Ethiopia").strip(),
            "job_type":    job_type or "Full-Time",
            "posted_date": posted_date,
            "deadline":    deadline or "N/A",
            "description": (description or "")[:600],
            "url":         job_url,
            "salary_text": None, "salary_min": None, "salary_max": None,
            "experience": None, "is_remote": False, "apply_url": None, "category": "NGO",
        }

    def _scrape_url(url: str):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
        except Exception as e:
            logger.warning("NGOJobs: GET failed %s: %s", url, e)
            return None
        return BeautifulSoup(r.text, "html.parser")

    for kw in KEYWORDS:
        page_url: Optional[str] = f"{JOBS_URL}?q={kw}"
        pages = 0
        while page_url and pages < MAX_PAGES:
            soup = _scrape_url(page_url)
            if not soup:
                break
            for link in soup.find_all("a", href=re.compile(r"^/jobs/[a-z0-9\-]+")):
                job = _parse_card(link)
                if job and job["job_id"] not in seen:
                    seen.add(job["job_id"])
                    jobs.append(job)
            nxt = soup.find("a", string=re.compile(r"^Next$", re.I))
            page_url = (BASE + nxt["href"] if nxt and nxt.get("href") else None)
            pages += 1
            time.sleep(1.2)

    logger.info("NGOJobs: %d jobs total", len(jobs))
    return jobs


# ===========================================================================
# SITE 5 — GeezJobs (geezjobs.com)
# ===========================================================================

def scrape_geezjobs() -> list[dict]:
    logger.info("GeezJobs: scraping...")
    BASE    = "https://geezjobs.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }

    def _fetch(url: str) -> Optional[str]:
        try:
            from curl_cffi import requests as cr
            r = cr.get(url, headers=HEADERS, impersonate="chrome124", timeout=20)
            if r.status_code == 200:
                return r.text
        except ImportError:
            pass
        except Exception:
            pass
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            logger.warning("GeezJobs: HTTP %d for %s (try: pip install curl-cffi)", r.status_code, url)
        except Exception as e:
            logger.warning("GeezJobs: %s", e)
        return None

    POSTED_RE = re.compile(r"^Posted\s+(Just now|Today|\d+\s+(?:hr|day|week)s?\s+ago)", re.I)

    def _company_from_slug(slug: str, title: str) -> str:
        title_slug  = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        title_words = title_slug.split("-")
        slug_words  = slug.split("-")
        end = 0
        for i, tw in enumerate(title_words):
            if i < len(slug_words) and slug_words[i] == tw:
                end = i + 1
            else:
                break
        rest = slug_words[end:]
        if not rest:
            return "Unknown"
        abbrevs = {"plc","sc","llc","ngo","un","undp","unicef","irc","who","inc"}
        return " ".join(w.upper() if w in abbrevs else w.title() for w in rest)

    jobs: list[dict] = []
    seen: set[str]   = set()

    MAX_PAGES = 3
    for page in range(1, MAX_PAGES + 1):
        url  = f"{BASE}/jobs-in-ethiopia" + (f"?page={page}" if page > 1 else "")
        html = _fetch(url)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")

        for link in soup.find_all("a", href=re.compile(r"^/job-detail/")):
            href  = link.get("href", "")
            slug  = href.replace("/job-detail/", "").strip("/")
            title = _clean(link.get_text())
            if (not title or len(title) < 3
                    or title.lower() in ("view details", "apply now", "view all jobs", "view")
                    or title.lower().startswith("featured")
                    or slug in seen):
                continue
            seen.add(slug)

            container = link
            for _ in range(5):
                parent = container.parent
                if parent is None or parent.name in ("body", "html"):
                    break
                if "Posted" in parent.get_text() or "Ethiopia" in parent.get_text():
                    container = parent
                    break
                container = parent

            ctext  = container.get_text(separator="\n")
            clines = [_clean(l) for l in ctext.splitlines() if _clean(l) and _clean(l) != title]

            posted   = None
            company  = None
            location = "Addis Ababa, Ethiopia"

            for line in clines:
                if POSTED_RE.match(line):
                    posted = re.sub(r"^[Pp]osted\s*", "", line).strip()
                elif re.search(r"Ethiopia|Addis Ababa", line):
                    location = line
                elif (len(line) > 2
                      and line not in ("Verified", "View Details")
                      and not re.match(r"^\d+\s*(hr|day|week|min)", line.lower())
                      and company is None):
                    company = line

            if not company:
                company = _company_from_slug(slug, title)

            jobs.append({
                "job_id":      f"geez_{slug}",
                "source":      "GeezJobs",
                "title":       title,
                "company":     company.strip(),
                "location":    location.strip(),
                "job_type":    "Full-Time",
                "deadline":    "N/A",
                "posted_date": posted,
                "description": "",
                "salary_text": None, "salary_min": None, "salary_max": None,
                "experience":  None, "url": f"{BASE}/job-detail/{slug}",
                "is_remote":   False, "apply_url": None, "category": "N/A",
            })

        if not soup.find("a", href=re.compile(r"^/job-detail/")):
            break
        time.sleep(1.2)

    logger.info("GeezJobs: enriching %d jobs with detail pages...", len(jobs))
    for job in jobs:
        html = _fetch(job["url"])
        if not html:
            time.sleep(1)
            continue
        soup  = BeautifulSoup(html, "html.parser")
        text  = soup.get_text(separator="\n")
        lines = [_clean(l) for l in text.splitlines() if _clean(l)]

        for i, line in enumerate(lines):
            nxt = lines[i+1] if i+1 < len(lines) else ""
            if line.lower() == "deadline" and nxt:
                job["deadline"] = re.sub(r"\s*\(\d+\s+days?\s+left\)", "", nxt).strip()
            if line.lower() == "experience" and nxt:
                job["experience"] = nxt
            if line.lower() == "posted" and nxt:
                job["posted_date"] = nxt

        sal_m = re.search(
            r"(\d[\d,\.]+\s*[-\u2013]\s*\d[\d,\.]+\s*(?:birr|ETB)|\d[\d,\.]+\s*(?:birr|ETB))",
            text, re.I,
        )
        if sal_m:
            job["salary_text"] = sal_m.group(1)

        h1 = soup.find("h1")
        if h1:
            parts = []
            for el in h1.find_all_next(["p", "li"]):
                t = _clean(el.get_text())
                if not t:
                    continue
                if any(kw in t.lower() for kw in ["how to apply", "apply for this", "similar jobs", "featured"]):
                    break
                parts.append(t)
            if parts:
                job["description"] = " ".join(parts)[:2000]

        time.sleep(0.8)

    logger.info("GeezJobs: %d jobs total", len(jobs))
    return jobs


# ===========================================================================
# ORCHESTRATOR
# ===========================================================================

def run_all_scrapers(sites: list[str] | None = None) -> list[dict]:
    SCRAPERS = {
        "hahu":  scrape_hahujobs,
        "ethio": scrape_ethiojobs,
        "palm":  scrape_palmjobs,
        "ngo":   scrape_ngojobs,
        "geez":  scrape_geezjobs,
    }

    to_run    = sites or list(SCRAPERS.keys())
    all_jobs: list[dict] = []
    seen:     set[str]   = set()

    for name in to_run:
        fn = SCRAPERS.get(name)
        if not fn:
            logger.warning("Unknown site key: %s", name)
            continue
        logger.info("=== Running scraper: %s ===", name.upper())
        try:
            jobs = fn()
        except Exception as e:
            logger.error("%s scraper crashed: %s", name, e)
            jobs = []

        for j in jobs:
            if j["job_id"] not in seen and _is_finance_job(j):
                seen.add(j["job_id"])
                j["scraped_at"] = datetime.now(timezone.utc).isoformat()
                all_jobs.append(j)

        logger.info("%s -> %d finance jobs after filter", name.upper(),
                    sum(1 for j in jobs if _is_finance_job(j)))

    logger.info("=== TOTAL: %d finance jobs from %s ===", len(all_jobs), ", ".join(to_run))
    return all_jobs


# ===========================================================================
# PRINT TO TERMINAL
# ===========================================================================

def print_jobs(jobs: list[dict]):
    if not jobs:
        print("\nNo finance jobs found.")
        return

    print(f"\n{'='*65}")
    print(f"  ETHIOPIAN FINANCE JOBS -- {len(jobs)} results  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"{'='*65}")

    by_source: dict[str, list] = {}
    for j in jobs:
        by_source.setdefault(j["source"], []).append(j)

    for source, src_jobs in by_source.items():
        print(f"\n  > {source}  ({len(src_jobs)} jobs)")
        print(f"  {'-'*60}")
        for i, j in enumerate(src_jobs, 1):
            print(f"\n  [{i}] {j['title']}")
            print(f"      Company  : {j['company']}")
            print(f"      Location : {j['location']}")
            print(f"      Type     : {j['job_type']}")
            print(f"      Posted   : {j.get('posted_date') or 'N/A'}")
            print(f"      Deadline : {j.get('deadline') or 'N/A'}")
            print(f"      Salary   : {j.get('salary_text') or 'N/A'}")
            print(f"      URL      : {j['url']}")


# ===========================================================================
# PUSH TO NEON POSTGRESQL
# ===========================================================================

def get_db_connection():
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError("psycopg2 not installed. Run: pip install psycopg2-binary")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set in .env file")
    return psycopg2.connect(db_url)


# ─────────────────────────────────────────────────────────────────────────────
# Content hash — used for duplicate detection across sources.
# Two jobs with identical (title + company + location) get the same hash,
# preventing the same vacancy scraped from multiple sites being inserted twice.
# ─────────────────────────────────────────────────────────────────────────────

def _make_content_hash(job: dict) -> str:
    import hashlib
    key = "|".join([
        (job.get("title")    or "").lower().strip(),
        (job.get("company")  or "").lower().strip(),
        (job.get("location") or "").lower().strip(),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _make_external_id(job: dict) -> str:
    """
    external_id = the scraper-assigned job_id (e.g. "geez_accountant-abc",
    "ngojobs_finance-officer-at-mercy-corps").  It is UNIQUE in the jobs table.
    """
    return job["job_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Salary range parser — converts "10000-20000birr" → (10000, 20000)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_salary_range(salary_text: str | None) -> tuple[int | None, int | None]:
    if not salary_text:
        return None, None
    nums = re.findall(r"\d[\d,]*", salary_text.replace(",", ""))
    nums = [int(n) for n in nums if n.isdigit()]
    if len(nums) >= 2:
        return min(nums), max(nums)
    if len(nums) == 1:
        return nums[0], None
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Push to the existing `jobs` table
# Schema (exact column names from your table):
#   id UUID PK, short_id SERIAL, external_id VARCHAR UNIQUE NOT NULL,
#   content_hash VARCHAR, linkedin_job_id VARCHAR,
#   company VARCHAR, title VARCHAR, url VARCHAR, source VARCHAR,
#   search_id UUID, description TEXT, location VARCHAR, remote BOOLEAN,
#   salary_min INTEGER, salary_max INTEGER, salary_source VARCHAR,
#   h1b_company_lca_count INTEGER, h1b_company_approval_rate DOUBLE PRECISION,
#   h1b_jd_flag BOOLEAN, h1b_jd_snippet VARCHAR, h1b_verdict VARCHAR,
#   cv_scores JSON, best_cv_score DOUBLE PRECISION, best_cv VARCHAR,
#   scoring_report JSON, cached_page_html TEXT, cached_page_text TEXT,
#   page_cached_at TIMESTAMPTZ, cache_error TEXT,
#   seen BOOLEAN, saved BOOLEAN, status VARCHAR,
#   discovered_at TIMESTAMPTZ
# ─────────────────────────────────────────────────────────────────────────────

def push_to_db(jobs: list[dict]) -> tuple[int, int]:
    """
    Upsert scraped jobs into the existing `jobs` table.

    Duplicate handling (two layers):
      1. external_id UNIQUE constraint — same scraper job_id → UPDATE not INSERT
      2. content_hash check — same title+company+location from a DIFFERENT source
         → skipped entirely (already exists under a different external_id)
    """
    if not jobs:
        return 0, 0

    try:
        import psycopg2
        import psycopg2.extras   # for UUID support
    except ImportError:
        raise RuntimeError("psycopg2 not installed. Run: pip install psycopg2-binary")

    conn = get_db_connection()
    cur  = conn.cursor()

    # ── Pre-load existing content_hashes to skip cross-source duplicates ──────
    cur.execute("SELECT content_hash FROM jobs WHERE content_hash IS NOT NULL;")
    existing_hashes: set[str] = {row[0] for row in cur.fetchall()}
    logger.info("DB: %d existing content hashes loaded", len(existing_hashes))

    inserted = updated = skipped = 0
    now = datetime.now(timezone.utc)

    for j in jobs:
        external_id  = _make_external_id(j)
        content_hash = _make_content_hash(j)

        # ── Cross-source dedup: same job already exists from another site ─────
        if content_hash in existing_hashes:
            skipped += 1
            logger.debug("DB: skip duplicate (content_hash match): %s", j.get("title"))
            continue

        # Parse salary into integer range
        sal_text = j.get("salary_text")
        sal_min, sal_max = _parse_salary_range(sal_text)
        # Prefer explicitly provided integers if available
        sal_min = j.get("salary_min") or sal_min
        sal_max = j.get("salary_max") or sal_max

        params = {
            "external_id":  external_id,
            "content_hash": content_hash,
            "company":      j.get("company"),
            "title":        j.get("title"),
            "url":          j.get("url"),
            "source":       j.get("source"),
            "description":  j.get("description") or None,
            "location":     j.get("location"),
            "remote":       bool(j.get("is_remote", False)),
            "salary_min":   sal_min,
            "salary_max":   sal_max,
            "salary_source": "scraped" if sal_text else None,
            # H1B fields — not applicable for Ethiopian jobs, left NULL
            "h1b_company_lca_count":    None,
            "h1b_company_approval_rate": None,
            "h1b_jd_flag":    None,
            "h1b_jd_snippet": None,
            "h1b_verdict":    None,
            # Scoring fields — populated later by JobNavigator AI
            "cv_scores":      None,
            "best_cv_score":  None,
            "best_cv":        None,
            "scoring_report": None,
            # Cache fields
            "cached_page_html": None,
            "cached_page_text": j.get("description") or None,
            "page_cached_at":   now if j.get("description") else None,
            "cache_error":      None,
            # Status fields
            "seen":          False,
            "saved":         False,
            "status":        "new",
            "discovered_at": now,
        }

        try:
            cur.execute("""
                INSERT INTO jobs (
                    id,
                    external_id,  content_hash,
                    company,      title,         url,          source,
                    description,  location,      remote,
                    salary_min,   salary_max,    salary_source,
                    h1b_company_lca_count, h1b_company_approval_rate,
                    h1b_jd_flag,  h1b_jd_snippet, h1b_verdict,
                    cv_scores,    best_cv_score,  best_cv,      scoring_report,
                    cached_page_html, cached_page_text, page_cached_at, cache_error,
                    seen,         saved,          status,       discovered_at
                ) VALUES (
                    gen_random_uuid(),
                    %(external_id)s,  %(content_hash)s,
                    %(company)s,      %(title)s,         %(url)s,      %(source)s,
                    %(description)s,  %(location)s,      %(remote)s,
                    %(salary_min)s,   %(salary_max)s,    %(salary_source)s,
                    %(h1b_company_lca_count)s, %(h1b_company_approval_rate)s,
                    %(h1b_jd_flag)s,  %(h1b_jd_snippet)s, %(h1b_verdict)s,
                    %(cv_scores)s,    %(best_cv_score)s,  %(best_cv)s,  %(scoring_report)s,
                    %(cached_page_html)s, %(cached_page_text)s,
                    %(page_cached_at)s,   %(cache_error)s,
                    %(seen)s,         %(saved)s,          %(status)s,   %(discovered_at)s
                )
                ON CONFLICT (external_id) DO UPDATE SET
                    content_hash      = EXCLUDED.content_hash,
                    title             = EXCLUDED.title,
                    company           = EXCLUDED.company,
                    description       = COALESCE(EXCLUDED.description, jobs.description),
                    location          = EXCLUDED.location,
                    salary_min        = COALESCE(EXCLUDED.salary_min,  jobs.salary_min),
                    salary_max        = COALESCE(EXCLUDED.salary_max,  jobs.salary_max),
                    cached_page_text  = COALESCE(EXCLUDED.cached_page_text, jobs.cached_page_text),
                    page_cached_at    = EXCLUDED.page_cached_at,
                    status            = CASE
                                          WHEN jobs.status IN ('applied','interview','offer','rejected')
                                          THEN jobs.status
                                          ELSE 'new'
                                        END
                RETURNING (xmax = 0) AS was_inserted;
            """, params)

            row = cur.fetchone()
            if row and row[0]:
                inserted += 1
                existing_hashes.add(content_hash)   # prevent same-run duplicates
            else:
                updated += 1

        except Exception as e:
            logger.error("DB: failed to insert %s (%s): %s", external_id, j.get("title"), e)
            conn.rollback()
            continue

    conn.commit()
    cur.close()
    conn.close()
    logger.info(
        "DB push complete — inserted: %d  updated: %d  skipped (duplicates): %d",
        inserted, updated, skipped,
    )
    return inserted, updated


# ===========================================================================
# SCHEDULER
# ===========================================================================

def run_once(sites: list[str] | None, push: bool):
    start = time.time()
    logger.info("Start scrape cycle at %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    jobs = run_all_scrapers(sites)
    print_jobs(jobs)
    if push:
        try:
            ins, upd = push_to_db(jobs)
            logger.info("DB push complete → jobs table: %d new, %d updated", ins, upd)
        except Exception as e:
            logger.error("DB push failed: %s", e)
    logger.info("Cycle done in %.1fs", time.time() - start)
    return jobs


def run_scheduler(sites: list[str] | None, interval_hours: float = 6.0):
    interval = int(interval_hours * 3600)
    logger.info("Scheduler started -- running every %gh. Press Ctrl+C to stop.", interval_hours)
    while True:
        try:
            run_once(sites, push=True)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user.")
            break
        except Exception as e:
            logger.error("Cycle failed: %s -- retrying in 30 min", e)
            time.sleep(1800)
            continue
        logger.info("Next run in %gh (%s)", interval_hours,
                    datetime.fromtimestamp(time.time() + interval).strftime("%H:%M:%S"))
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped.")
            break


# ===========================================================================
# CLI
# ===========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ethiopian Finance Job Scraper")
    parser.add_argument("--push",     action="store_true", help="Push results to Neon PostgreSQL jobs table")
    parser.add_argument("--schedule", action="store_true", help="Run every 6 hours")
    parser.add_argument("--hours",    type=float, default=6.0, help="Scheduler interval in hours (default: 6)")
    parser.add_argument("--site",     nargs="+",  help="Scrape specific sites only: hahu ethio palm ngo geez")
    args = parser.parse_args()

    if args.schedule:
        run_scheduler(args.site, interval_hours=args.hours)
    else:
        run_once(args.site, push=args.push)
