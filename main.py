"""
I-LEAD Mission Scraper
Scraping des missions IT sur LittleBig Connection (9 catégories), HiTechPros et LinkedIn (via jobquest).
Notifications Discord à chaque nouvelle offre.
Filtre intégré : ne conserve que les offres de SOUS-TRAITANCE.
"""

import argparse
import asyncio
import hashlib
import html as html_lib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import (
    urlparse, urldefrag, urljoin, parse_qs, unquote, urlencode
)

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    async_playwright = None
    PLAYWRIGHT_AVAILABLE = False

try:
    from jobquest import scrape_jobs
    JOBQUEST_AVAILABLE = True
except ImportError:
    scrape_jobs = None
    JOBQUEST_AVAILABLE = False


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "missions.db"
DEBUG_DIR = ROOT / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

if not CONFIG_PATH.exists():
    raise FileNotFoundError("config.json introuvable.")

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

INTERVAL = max(60, int(CONFIG.get("interval_seconds", 180)))
MAX_PAGES = max(1, int(CONFIG.get("max_pages_per_site", 5)))
MAX_CONCURRENT = max(1, int(CONFIG.get("max_concurrent_sites", 3)))
MIN_HTML_CHARS = int(CONFIG.get("min_html_chars", 300))
BASELINE_FIRST_RUN = bool(CONFIG.get("baseline_on_first_run", False))
USER_AGENT = CONFIG.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DISCORD_CONCURRENCY = max(1, int(os.getenv("DISCORD_CONCURRENCY", "5")))
DISCORD_MAX_PER_CYCLE = max(1, int(os.getenv("DISCORD_MAX_PER_CYCLE", "40")))

# ============================================================
# CONFIGURATION DU FILTRE SOUS-TRAITANCE
# ============================================================

# Active ou désactive le filtre global
SUBCONTRACTING_FILTER_ENABLED = os.getenv(
    "SUBCONTRACTING_FILTER", "true"
).lower() in ("true", "1", "yes", "on")

# Sources dont le modèle EST nativement la sous-traitance inter-ESN.
SUBCONTRACTING_SOURCES = {
    "hitechpros",   # Bourse d'intercontrats ESN
    "turnover_it",  # Marketplace inter-ESN / intercontrats
}

# Marqueurs textuels (titre + description)
# Intègre les mots-clés LinkedIn fournis et le vocabulaire réel des offres.
SUBCONTRACTING_MARKERS = [
    # === Termes explicites (confirmés par les sources) ===
    "sous-traitance", "sous traitant", "sous-traitant",
    "portage salarial", "portage", "sub-contracting", "subcontracting",
    "en sous-traitance", "mission sous-traitée",
    "prestation de sous-traitance", "apporteur d'affaires",
    
    # === Termes ESN / facturation (confirmés par les offres LinkedIn) ===
    "assistance technique", "mode de facturation",
    "régie", "forfait",
    "intercontrat", "inter-contrat",
    "co-traitance", "co-traitant",
    "mission entre esn", "partenariat esn", "partenaire esn",
    "renfort d'équipe", "renfort technique", "renfort projet",
    "renfort de delivery",
    
    # === Termes freelance / indépendants ===
    "mission freelance", "freelance / indépendant",
    "mission ouverte aux indépendants",
    "recherche prestataire", "besoin consultant",
    "business developer freelance",
    
    # === Vos mots-clés de recherche LinkedIn (intégrés comme marqueurs) ===
    "mission en sous-traitance", "mission en régie",
    "recherche partenaire", "partenariat commercial",
    "mission portage",
    
    # === Termes Turnover-IT (documentation) ===
    "offre ouverte aux esn",
    
    # === Termes génériques ESN (à utiliser avec prudence) ===
    "consultant indépendant",
]

# ============================================================
# LISTE DES SOURCES SUPPORTÉES
# ============================================================

LITTLEBIG_KEYS = {
    "littlebig_data_ai",
    "littlebig_cyber",
    "littlebig_infra",
    "littlebig_cloud",
    "littlebig_software",
    "littlebig_ux",
    "littlebig_transformation",
    "littlebig_rse",
    "littlebig_marketing",
}
LEGACY_LITTLEBIG_KEYS = {"littlebig_connection"}

WEB_SOURCE_KEYS = {"turnover_it", "hitechpros"} | LITTLEBIG_KEYS | LEGACY_LITTLEBIG_KEYS
ALLOWED_KEYS = WEB_SOURCE_KEYS | {"linkedin"}

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}
TIMEOUT = aiohttp.ClientTimeout(total=45, connect=15, sock_read=30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger("ILEAD-MISSION-SCRAPER")


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    with db_connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS scan_state (
                platform TEXT PRIMARY KEY,
                last_scan TEXT,
                last_count INTEGER DEFAULT 0,
                last_pages INTEGER DEFAULT 0,
                last_error TEXT
            )
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(scan_state)").fetchall()}
        if "last_pages" not in columns:
            db.execute("ALTER TABLE scan_state ADD COLUMN last_pages INTEGER DEFAULT 0")
            log.info("[DATABASE] Colonne last_pages ajoutée.")

        db.execute("""
            CREATE TABLE IF NOT EXISTS platform_state (
                platform TEXT PRIMARY KEY,
                baseline_done INTEGER DEFAULT 0
            )
        """)
        db.commit()
    log.info("[DATABASE] Base SQLite initialisée.")


def mission_exists(mid):
    with db_connect() as db:
        return db.execute(
            "SELECT notified FROM missions WHERE id=?", (mid,)
        ).fetchone()


def save_mission(item):
    with db_connect() as db:
        db.execute("""
            INSERT OR IGNORE INTO missions
            (id, platform, title, url, first_seen, notified)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (
            item["id"], item["platform"], item["title"], item["url"],
            datetime.now(timezone.utc).isoformat()
        ))
        db.commit()


def set_notified(mid):
    with db_connect() as db:
        db.execute("UPDATE missions SET notified=1 WHERE id=?", (mid,))
        db.commit()


def baseline_is_done(platform):
    with db_connect() as db:
        row = db.execute(
            "SELECT baseline_done FROM platform_state WHERE platform=?",
            (platform,)
        ).fetchone()
        return bool(row and row[0])


def set_baseline_done(platform):
    with db_connect() as db:
        db.execute("""
            INSERT INTO platform_state(platform, baseline_done) VALUES (?, 1)
            ON CONFLICT(platform) DO UPDATE SET baseline_done=1
        """, (platform,))
        db.commit()


def update_scan(platform, count, pages=0, error=None):
    with db_connect() as db:
        db.execute("""
            INSERT INTO scan_state(platform,last_scan,last_count,last_pages,last_error)
            VALUES (?,?,?,?,?)
            ON CONFLICT(platform) DO UPDATE SET
                last_scan=excluded.last_scan,
                last_count=excluded.last_count,
                last_pages=excluded.last_pages,
                last_error=excluded.last_error
        """, (
            platform, datetime.now(timezone.utc).isoformat(),
            count, pages, str(error)[:1000] if error else None
        ))
        db.commit()


# ============================================================
# FILTRE SOUS-TRAITANCE
# ============================================================

def is_likely_subcontracting(item, site_key):
    """
    Détermine si une offre est de la sous-traitance.
    
    Stratégie :
    1. Marqueurs textuels explicites (titre + description) → True
    2. Sources natives (HiTechPros, Turnover-IT) → True par défaut
    3. LittleBig et LinkedIn → False par défaut
    """
    title = (item.get("title") or "").lower()
    description = (item.get("description") or "").lower()
    text = f"{title} {description}"
    
    # Étape 1 : marqueurs explicites
    if any(marker in text for marker in SUBCONTRACTING_MARKERS):
        return True
    
    # Étape 2 : sources dont le modèle EST la sous-traitance inter-ESN
    if site_key in SUBCONTRACTING_SOURCES:
        return True
    
    # Étape 3 : rejet par défaut
    return False


def apply_subcontracting_filter(platform_key, items):
    """Filtre une liste d'items selon le critère sous-traitance."""
    if not SUBCONTRACTING_FILTER_ENABLED:
        return items
    
    if not items:
        return items
    
    before = len(items)
    filtered = [item for item in items if is_likely_subcontracting(item, platform_key)]
    after = len(filtered)
    
    if before != after:
        log.info(
            "[%s] Filtre sous-traitance : %d → %d offres (%d ignorées)",
            platform_key, before, after, before - after
        )
    
    if after == 0:
        log.warning(
            "[%s] Aucune offre de sous-traitance retenue sur %d collectées. "
            "Vérifiez les marqueurs ou désactivez SUBCONTRACTING_FILTER.",
            platform_key, before
        )
    
    return filtered


# ============================================================
# URL / VALIDATION
# ============================================================

def clean_text(value):
    return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip()


def canonical_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    url = urldefrag(url)[0]
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in list(query):
        if key.lower().startswith(("utm_", "trk", "tracking")) or key.lower() in {
            "ref", "source", "fbclid", "gclid"
        }:
            query.pop(key, None)
    return parsed._replace(query=urlencode(query, doseq=True)).geturl().rstrip("/")


def make_id(url):
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def domain_allowed(url, site):
    host = (urlparse(url).hostname or "").lower()
    allowed = [x.lower().strip() for x in site.get("allowed_domains", [])]
    return bool(host and any(host == d or host.endswith("." + d) for d in allowed))


def is_forbidden_url(url, site):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    if not domain_allowed(url, site):
        return True
    path = (parsed.path or "").lower()
    forbidden = site.get("forbidden_path_keywords", [
        "logout", "signout", "login", "signin", "register",
        "signup", "contact", "privacy", "cookie", "password"
    ])
    if any(word.lower() in path for word in forbidden):
        return True

    exclude_regex = site.get("candidate_url_exclude_regex", "")
    if exclude_regex:
        try:
            if re.search(exclude_regex, parsed.path + "?" + parsed.query, re.I):
                return True
        except re.error:
            log.exception("Regex candidate_url_exclude_regex invalide pour %s", site.get("name"))

    regex = site.get("candidate_url_regex", "")
    if regex:
        try:
            if not re.search(regex, parsed.path + "?" + parsed.query, re.I):
                return True
        except re.error:
            log.exception("Regex candidate_url_regex invalide pour %s", site.get("name"))
            return True
    return False


def valid_title(title, site):
    title = clean_text(title)
    if len(title) < int(site.get("min_title_length", 8)):
        return False
    blocked = {
        "accueil", "home", "connexion", "se connecter", "login",
        "en savoir plus", "lire la suite", "contact", "suivant",
        "précédent", "next", "previous", "voir la mission", "voir l'offre"
    }
    return title.lower() not in blocked


def save_debug(site_key, page_no, html_text, metadata, diagnostic=None):
    folder = DEBUG_DIR / site_key
    folder.mkdir(parents=True, exist_ok=True)
    suffix = f"page_{page_no}"
    (folder / f"{suffix}.html").write_text(html_text, encoding="utf-8")
    (folder / f"{suffix}_info.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if diagnostic is not None:
        (folder / f"{suffix}_links.json").write_text(
            json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ============================================================
# FETCHERS (aiohttp / playwright)
# ============================================================

async def fetch_http(session, site_key, site, url, page_no):
    headers = HEADERS.copy()
    headers.update(site.get("headers", {}))
    async with session.get(
        url,
        headers=headers,
        cookies=site.get("cookies", {}),
        allow_redirects=True
    ) as response:
        body = await response.text(errors="replace")
        final_url = str(response.url)
        ctype = response.headers.get("Content-Type", "").lower()

        if response.status in (401, 403, 429):
            raise RuntimeError(f"HTTP {response.status}: accès refusé/limité.")
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        if len(body) < MIN_HTML_CHARS:
            raise RuntimeError(f"Réponse trop courte ({len(body)} caractères).")
        if "text/html" not in ctype and "application/xhtml" not in ctype:
            raise RuntimeError(f"Content-Type non HTML: {ctype}")

        metadata = {
            "requested_url": url, "final_url": final_url,
            "status": response.status, "content_type": ctype,
            "length": len(body), "method": "aiohttp",
            "captured_at": datetime.now(timezone.utc).isoformat()
        }
        save_debug(site_key, page_no, body, metadata)
        return body, final_url


async def fetch_browser(site_key, site, url, page_no):
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright absent: pip install playwright")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=site.get("headers", {}).get("User-Agent", USER_AGENT),
                locale="fr-FR"
            )
            cookies = site.get("cookies", {})
            if cookies:
                host = urlparse(url).hostname
                await context.add_cookies([
                    {
                        "name": str(k), "value": str(v), "domain": host,
                        "path": "/", "secure": url.startswith("https://")
                    } for k, v in cookies.items()
                ])
            page = await context.new_page()
            response = await page.goto(
                url,
                wait_until=site.get("wait_until", "networkidle"),
                timeout=int(site.get("navigation_timeout_ms", 45000))
            )
            wait_selector = site.get("wait_for_selector", "")
            if wait_selector:
                try:
                    await page.wait_for_selector(
                        wait_selector,
                        timeout=int(site.get("selector_timeout_ms", 15000))
                    )
                except Exception:
                    log.warning("[%s] Sélecteur d'attente absent: %s", site_key, wait_selector)
            else:
                await page.wait_for_timeout(int(site.get("render_wait_ms", 3000)))

            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
            except Exception:
                pass

            body = await page.content()
            final_url = page.url
            status = response.status if response else 0
            if status in (401, 403, 429):
                raise RuntimeError(f"Playwright HTTP {status}: accès refusé/limité.")
            if status >= 400:
                raise RuntimeError(f"Playwright HTTP {status}")
            if len(body) < MIN_HTML_CHARS:
                raise RuntimeError(f"HTML Playwright trop court: {len(body)} caractères")

            metadata = {
                "requested_url": url, "final_url": final_url,
                "status": status, "length": len(body),
                "method": "playwright",
                "captured_at": datetime.now(timezone.utc).isoformat()
            }
            save_debug(site_key, page_no, body, metadata)
            await context.close()
            return body, final_url
        finally:
            await browser.close()


async def fetch_page(session, site_key, site, url, page_no):
    if site.get("render_js", False):
        return await fetch_browser(site_key, site, url, page_no)
    return await fetch_http(session, site_key, site, url, page_no)


# ============================================================
# EXTRACTION + PAGINATION
# ============================================================

def first_text(node, selectors):
    for selector in selectors:
        try:
            found = node.select_one(selector)
            if found:
                value = clean_text(found.get_text(" ", strip=True))
                if value:
                    return value
        except Exception:
            continue
    return ""


TITLE_SUFFIX_CLEANUP = re.compile(
    r"\s*(Voir la mission|Voir l'offre|Voir l’offre|En savoir plus|Lire la suite|Lire la mission)\s*$",
    re.I
)


def extract_cards(html_text, page_url, site_key, site, page_no):
    soup = BeautifulSoup(html_text, "html.parser")

    card_selectors = site.get("card_selectors", [])
    title_selectors = site.get("title_selectors", [])
    candidate_selectors = site.get("candidate_selectors", ["a[href]"])

    if isinstance(card_selectors, str):
        card_selectors = [card_selectors] if card_selectors else []
    if isinstance(title_selectors, str):
        title_selectors = [title_selectors] if title_selectors else []
    if isinstance(candidate_selectors, str):
        candidate_selectors = [candidate_selectors]

    cards = []

    if card_selectors:
        for selector in card_selectors:
            try:
                cards.extend(soup.select(selector))
            except Exception:
                log.exception("[%s] Sélecteur carte invalide: %s", site_key, selector)

    if not cards:
        for selector in candidate_selectors:
            try:
                cards.extend(soup.select(selector))
            except Exception:
                log.exception("[%s] Sélecteur invalide: %s", site_key, selector)

    candidate_count = len(cards)

    found, seen = [], set()
    rejected = {"no_href": 0, "bad_url": 0, "bad_title": 0, "duplicate": 0}
    preview = []

    for card in cards:
        href = None
        link_element = None

        if card.name == "a" and card.get("href"):
            href = card.get("href")
            link_element = card
        else:
            a_tag = card.find("a", href=True)
            if a_tag:
                href = a_tag.get("href")
                link_element = a_tag
            else:
                for attr in ("href", "data-href", "data-url", "data-redirect"):
                    if card.get(attr):
                        href = card.get(attr)
                        link_element = card
                        break

        if not href:
            rejected["no_href"] += 1
            continue

        url = canonical_url(urljoin(page_url, href.strip()))
        if is_forbidden_url(url, site):
            rejected["bad_url"] += 1
            continue

        title = first_text(card, title_selectors)

        if not title and link_element is not None:
            title = clean_text(
                link_element.get_text(" ", strip=True)
                or link_element.get("aria-label", "")
                or link_element.get("title", "")
            )

        if not title:
            title = clean_text(card.get_text(" ", strip=True))

        title = TITLE_SUFFIX_CLEANUP.sub("", title).strip()

        if not valid_title(title, site):
            rejected["bad_title"] += 1
            continue

        mid = make_id(url)
        if mid in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(mid)

        item = {
            "id": mid,
            "platform": site["name"],
            "title": title[:500],
            "url": url
        }
        found.append(item)
        if len(preview) < 100:
            preview.append({"title": title, "url": url})

    diagnostic = {
        "platform": site["name"], "page_url": page_url,
        "card_selectors": card_selectors,
        "candidate_selectors": candidate_selectors,
        "anchor_count": len(soup.find_all("a", href=True)),
        "candidate_count": candidate_count,
        "missions_count": len(found), "rejected": rejected,
        "preview": preview,
        "captured_at": datetime.now(timezone.utc).isoformat()
    }
    save_debug(site_key, page_no, html_text, {
        "page_url": page_url, "method": "extraction"
    }, diagnostic)
    log.info(
        "[%s] page %d | ancres=%d candidats=%d offres=%d rejets=%s",
        site_key, page_no, diagnostic["anchor_count"],
        candidate_count, len(found), rejected
    )
    return found, soup


def find_next_page(soup, current_url, site):
    selectors = site.get("next_page_selectors", [])
    if isinstance(selectors, str):
        selectors = [selectors] if selectors else []
    for selector in selectors:
        try:
            node = soup.select_one(selector)
            if node:
                href = node.get("href") or node.get("data-href")
                if href:
                    next_url = urljoin(current_url, href)
                    if next_url != current_url:
                        return next_url
        except Exception:
            continue
    return None


async def scan_site(site_key, site, session, semaphore):
    async with semaphore:
        try:
            start_url = site.get("listing_url") or site.get("start_url")
            if not start_url:
                raise RuntimeError("listing_url/start_url absent dans config.json")

            all_items, visited = {}, set()
            current_url = start_url
            pages_scanned = 0

            max_pages = min(MAX_PAGES, int(site.get("max_pages", MAX_PAGES)))
            for page_no in range(1, max_pages + 1):
                if not current_url or current_url in visited:
                    break
                visited.add(current_url)

                body, final_url = await fetch_page(
                    session, site_key, site, current_url, page_no
                )
                items, soup = extract_cards(
                    body, final_url, site_key, site, page_no
                )
                for item in items:
                    all_items[item["id"]] = item
                pages_scanned += 1

                next_url = find_next_page(soup, final_url, site)
                if not next_url:
                    break
                if not domain_allowed(next_url, site):
                    break
                current_url = next_url

            result = list(all_items.values())
            update_scan(site_key, len(result), pages_scanned)
            if not result:
                log.warning("[%s] 0 offre. Examiner debug/%s/", site_key, site_key)
            return result
        except Exception as exc:
            update_scan(site_key, 0, 0, exc)
            log.exception("[%s] Scraping échoué", site_key)
            return None


# ============================================================
# LINKEDIN VIA JOBQUEST
# ============================================================

def collect_linkedin_jobs():
    if not JOBQUEST_AVAILABLE:
        log.error("[LinkedIn] jobquest non installé. Lance: pip install jobquest")
        return []

    # Vos 10 requêtes prioritaires pour LinkedIn
    search_terms = os.getenv(
        "LINKEDIN_SEARCH_TERMS",
        "mission sous-traitance,mission freelance,recherche prestataire IT,"
        "mission régie,besoin consultant,mission ouverte indépendants,"
        "partenariat ESN,recherche partenaire ESN,renfort équipe IT,"
        "mission portage salarial"
    ).split(",")
    location = os.getenv("LINKEDIN_LOCATION", "France")
    results_wanted = int(os.getenv("LINKEDIN_RESULTS", "20"))

    # Mots-clés de filtrage des titres (pour ne garder que les offres pertinentes)
    keywords = [
        k.strip().lower()
        for k in os.getenv(
            "LINKEDIN_TITLE_KEYWORDS",
            "sous-traitance,prestataire,prestataire it,régie,forfait,"
            "portage,portage salarial,partenariat esn,partenaire esn,"
            "renfort,renfort équipe,renfort it,besoin consultant,"
            "mission freelance,mission indépendant,consultant indépendant,"
            "freelance,indépendant"
        ).split(",")
        if k.strip()
    ]

    all_items = []
    for term in search_terms:
        term = term.strip()
        if not term:
            continue
        try:
            log.info("[LinkedIn] Recherche: '%s' à %s", term, location)
            df = scrape_jobs(
                site_name="linkedin",
                search_term=term,
                location=location,
                results_wanted=results_wanted,
                linkedin_fetch_description=False,
            )
            if df is None or df.empty:
                log.warning("[LinkedIn] Aucun résultat pour '%s'", term)
                continue

            kept, skipped = 0, 0
            for _, row in df.iterrows():
                url = str(row.get("job_url", "")).strip()
                title = str(row.get("title", "")).strip()
                if not url or not title:
                    continue
                # Filtre par mots-clés dans le titre
                if keywords and not any(k in title.lower() for k in keywords):
                    skipped += 1
                    continue
                url = canonical_url(url)
                all_items.append({
                    "id": make_id(url),
                    "platform": "LinkedIn",
                    "title": title[:500],
                    "url": url,
                })
                kept += 1
            log.info("[LinkedIn] '%s' → %d gardées, %d filtrées", term, kept, skipped)
        except Exception:
            log.exception("[LinkedIn] Erreur lors de la recherche '%s'", term)

    seen = set()
    unique = []
    for item in all_items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)

    log.info("[LinkedIn] %d offre(s) unique(s) trouvée(s)", len(unique))
    return unique


# ============================================================
# DISCORD + PROCESSING
# ============================================================

async def discord_notify(session, item):
    if not WEBHOOK:
        return False
    payload = {
        "username": "I-LEAD Mission Intelligence",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "Nouvelle mission de sous-traitance IT",
            "description": item["title"][:4000],
            "url": item["url"],
            "color": 3066993,
            "fields": [{
                "name": "Plateforme", "value": item["platform"][:100],
                "inline": True
            }],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }
    for attempt in range(3):
        try:
            async with session.post(WEBHOOK, json=payload) as response:
                if response.status in (200, 204):
                    return True
                if response.status == 429:
                    try:
                        data = await response.json()
                        delay = float(data.get("retry_after", 2))
                    except Exception:
                        delay = 2
                    await asyncio.sleep(min(delay, 20))
                    continue
                body = await response.text()
                if response.status >= 500:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"Discord HTTP {response.status}: {body[:300]}")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    return False


async def process_items(platform_key, items, session):
    if items is None:
        return
    
    # ============================================
    # FILTRE SOUS-TRAITANCE
    # ============================================
    items = apply_subcontracting_filter(platform_key, items)
    
    if not items:
        return
    
    # Baseline initiale (avant notifications)
    if BASELINE_FIRST_RUN and not baseline_is_done(platform_key):
        for item in items:
            save_mission(item)
        set_baseline_done(platform_key)
        log.info("[%s] Baseline initiale: %d offres (filtrées sous-traitance)", platform_key, len(items))
        return

    if not WEBHOOK:
        for item in items:
            if mission_exists(item["id"]) is None:
                save_mission(item)
        return

    to_notify = []
    for item in items:
        existing = mission_exists(item["id"])
        if existing is None:
            save_mission(item)
            to_notify.append((item, "new"))
        elif existing[0] == 0:
            to_notify.append((item, "resend"))

    if not to_notify:
        return

    if len(to_notify) > DISCORD_MAX_PER_CYCLE:
        log.warning(
            "[%s] %d notifications en attente, limitées à %d ce cycle. "
            "Le reste sera envoyé au prochain cycle.",
            platform_key, len(to_notify), DISCORD_MAX_PER_CYCLE
        )
        to_notify = to_notify[:DISCORD_MAX_PER_CYCLE]

    log.info("[%s] Envoi de %d notification(s) Discord...", platform_key, len(to_notify))

    sem = asyncio.Semaphore(DISCORD_CONCURRENCY)
    sent = 0
    failed = 0

    async def notify_one(item, kind):
        nonlocal sent, failed
        async with sem:
            if kind == "new":
                log.info("[%s] Nouvelle offre sous-traitance: %s", platform_key, item["title"])
            try:
                if await discord_notify(session, item):
                    set_notified(item["id"])
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                log.exception("[%s] Notification Discord en échec", platform_key)

    await asyncio.gather(*[notify_one(item, kind) for item, kind in to_notify])
    log.info("[%s] Notifications: %d envoyées, %d échecs", platform_key, sent, failed)


# ============================================================
# MAIN LOOP
# ============================================================

async def run_cycle(web_sites, linkedin_enabled, session, semaphore):
    started = asyncio.get_running_loop().time()

    tasks = [
        scan_site(key, site, session, semaphore)
        for key, site in web_sites.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for (key, _site), result in zip(web_sites.items(), results):
        if isinstance(result, Exception):
            log.exception("[%s] Erreur inattendue: %s", key, result)
            update_scan(key, 0, 0, result)
        else:
            await process_items(key, result, session)

    if linkedin_enabled:
        linkedin_items = await asyncio.to_thread(collect_linkedin_jobs)
        update_scan("linkedin", len(linkedin_items), 0)
        await process_items("linkedin", linkedin_items, session)

    elapsed = asyncio.get_running_loop().time() - started
    return elapsed


async def monitor(once=False):
    init_db()
    all_sites = CONFIG.get("sites", {})

    unknown = set(all_sites) - ALLOWED_KEYS
    if unknown:
        log.warning("Sources ignorées (non supportées): %s", ", ".join(sorted(unknown)))

    web_sites = {
        key: value for key, value in all_sites.items()
        if key in WEB_SOURCE_KEYS
        and value.get("enabled", False)
        and value.get("collection", "web") == "web"
    }

    linkedin_enabled = bool(
        all_sites.get("linkedin", {}).get("enabled", True)
        and all_sites.get("linkedin", {}).get("collection", "web") == "web"
    )

    if not web_sites and not linkedin_enabled:
        raise RuntimeError("Aucune source activée dans config.json.")

    connector = aiohttp.TCPConnector(limit=20, ssl=True)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with aiohttp.ClientSession(connector=connector, timeout=TIMEOUT) as session:
        log.info("=" * 65)
        log.info("I-LEAD MISSION SCRAPER — FILTRE SOUS-TRAITANCE")
        log.info("Sites web actifs (%d): %s", len(web_sites), ", ".join(web_sites.keys()) or "aucun")
        log.info("LinkedIn (jobquest): %s", linkedin_enabled)
        log.info("Intervalle: %ss | max pages/site: %s", INTERVAL, MAX_PAGES)
        log.info("Playwright disponible: %s", PLAYWRIGHT_AVAILABLE)
        log.info("jobquest disponible: %s", JOBQUEST_AVAILABLE)
        log.info("Webhook Discord: %s", "configuré" if WEBHOOK else "ABSENT")
        log.info("Limite notifications/cycle: %d", DISCORD_MAX_PER_CYCLE)
        log.info("Filtre sous-traitance: %s", "ACTIF" if SUBCONTRACTING_FILTER_ENABLED else "DÉSACTIVÉ")
        log.info("Sources sous-traitance natives: %s", ", ".join(sorted(SUBCONTRACTING_SOURCES)))
        log.info("Mode: %s", "once" if once else "boucle")
        log.info("=" * 65)

        while True:
            elapsed = await run_cycle(web_sites, linkedin_enabled, session, semaphore)
            if once:
                log.info("Cycle unique terminé en %.1fs.", elapsed)
                break
            delay = max(5, INTERVAL - elapsed)
            log.info("Cycle terminé en %.1fs. Prochain cycle dans %.1fs.", elapsed, delay)
            await asyncio.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description="I-LEAD Mission Scraper")
    parser.add_argument("--once", action="store_true", help="Lance un seul cycle puis quitte")
    parser.add_argument("--clear-db", action="store_true", help="Vide la base missions.db avant de lancer")
    parser.add_argument(
        "--no-filter", action="store_true",
        help="Désactive le filtre sous-traitance pour ce run"
    )
    args = parser.parse_args()

    if args.no_filter:
        global SUBCONTRACTING_FILTER_ENABLED
        SUBCONTRACTING_FILTER_ENABLED = False
        log.warning("Filtre sous-traitance DÉSACTIVÉ via --no-filter")

    if args.clear_db:
        if DB_PATH.exists():
            DB_PATH.unlink()
            log.info("Base supprimée: %s", DB_PATH)

    try:
        asyncio.run(monitor(once=args.once))
    except KeyboardInterrupt:
        log.info("Arrêt manuel.")
    except Exception:
        log.exception("Erreur fatale.")
    finally:
        try:
            loop = asyncio.new_event_loop()
            loop.close()
        except Exception:
            pass
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    main()