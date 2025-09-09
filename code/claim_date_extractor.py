!pip install aiohttp lxml tqdm pandas beautifulsoup4 fake-useragent nest_asyncio python-dateutil aiolimiter -q

import os
import re
import json
import random
import logging
import nest_asyncio
import pandas as pd
import aiohttp
import asyncio
import aiolimiter
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from urllib.parse import urlparse
from dateutil import parser
from datetime import datetime, date
from calendar import monthrange
from tqdm.notebook import tqdm
from functools import lru_cache
import time
import pickle

nest_asyncio.apply()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# -----------------------
# Configuration - ORIGINAL (MAX SUCCESS RATE)
# -----------------------
class Config:
    INPUT_FILE = "/content/FACTors.csv"
    RULES_FILE = "/content/top_10_urls_per_organisation - top_10_urls_per_organisation.csv (2).csv"
    ORG_COL = "organisation"
    URL_COL = "url"
    ARTICLE_DATE_COL = "date_published"
    CHECKPOINT_FILE = "processing_checkpoint.pkl"
    RESULTS_FILE = "partial_results.csv"

    # Networking / concurrency - ORIGINAL SETTINGS
    MAX_CONCURRENT_REQUESTS = 20
    CONCURRENCY = 10
    REQUEST_TIMEOUT = 30
    RETRY_ATTEMPTS = 3
    DELAY_MIN = 0.8
    DELAY_MAX = 1.6
    RATE_LIMIT_REQUESTS = 10
    RATE_LIMIT_PERIOD = 1

    # Recency / validation
    MAX_DAYS_BEFORE_PUB = 31
    MAX_DAYS_FROM_NOW = 365 * 2
    REQUIRE_BEFORE_ARTICLE = False
    ALLOW_SAME_DAY_AS_ARTICLE = True
    ENFORCE_DIFFERENT_FROM_ARTICLE = False

    # Partial dates
    ACCEPT_MONTH_YEAR = True
    ACCEPT_YEAR_ONLY = True
    PARTIAL_DATE_NORMALIZATION = "start"

    # Organization special rules
    EXCLUDE_ORGS = {"stage media liberia"}
    FORCE_CLAIM_EQ_PUB_ORGS = {"digiteye india | fact checkers"}

    # Performance optimizations
    MAX_HTML_SIZE = 2 * 1024 * 1024
    CACHE_SIZE = 1000
    CHECKPOINT_INTERVAL = 25  # Save every 25 URLs

# -----------------------
# Checkpoint Management
# -----------------------
def save_checkpoint(processed_count, results, total_urls):
    """Save current progress to checkpoint file"""
    checkpoint_data = {
        'processed_count': processed_count,
        'results': results,
        'total_urls': total_urls,
        'timestamp': datetime.now().isoformat()
    }
    with open(Config.CHECKPOINT_FILE, 'wb') as f:
        pickle.dump(checkpoint_data, f)
    print(f"💾 Checkpoint saved: {processed_count}/{total_urls} URLs processed")

def load_checkpoint():
    """Load previous progress from checkpoint file"""
    if os.path.exists(Config.CHECKPOINT_FILE):
        try:
            with open(Config.CHECKPOINT_FILE, 'rb') as f:
                data = pickle.load(f)
            print(f"🔄 Resuming from checkpoint: {data['processed_count']}/{data['total_urls']} URLs")
            return data
        except:
            print("⚠️  Checkpoint file corrupted, starting from scratch")
            return None
    return None

def save_partial_results(results):
    """Save partial results to CSV"""
    if results:
        results_df = pd.DataFrame(results)
        results_df.to_csv(Config.RESULTS_FILE, index=False)
        print(f"📊 Partial results saved: {len(results)} rows")

def load_partial_results():
    """Load partial results if they exist"""
    if os.path.exists(Config.RESULTS_FILE):
        try:
            results_df = pd.read_csv(Config.RESULTS_FILE)
            return results_df.to_dict('records')
        except:
            print("⚠️  Partial results file corrupted")
    return []

# -----------------------
# CSV loading & rules - ORIGINAL (KEEP ALL YOUR LOGIC)
# -----------------------
def load_csv_with_fallbacks(path):
    encodings = ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252', 'utf-8-sig']
    for e in encodings:
        try:
            return pd.read_csv(path, encoding=e, on_bad_lines='skip')
        except Exception:
            continue
    return pd.read_csv(path, encoding='utf-8', errors='replace')

if not os.path.exists(Config.INPUT_FILE):
    raise FileNotFoundError(f"Input file not found: {Config.INPUT_FILE}")

df = load_csv_with_fallbacks(Config.INPUT_FILE)

# Auto-detect article date column if missing
if Config.ARTICLE_DATE_COL not in df.columns:
    candidates = [c for c in df.columns if re.search(r"(date|publish)", c, re.I)]
    if candidates:
        df.rename(columns={candidates[0]: Config.ARTICLE_DATE_COL}, inplace=True)
        print(f"Using '{candidates[0]}' as article date column -> '{Config.ARTICLE_DATE_COL}'")
    else:
        raise ValueError("Could not find article date column. Set Config.ARTICLE_DATE_COL appropriately.")

for col in (Config.ORG_COL, Config.URL_COL, Config.ARTICLE_DATE_COL):
    if col not in df.columns:
        raise ValueError(f"Required column missing: {col}")

# Normalize org strings and filter excluded orgs
df[Config.ORG_COL] = df[Config.ORG_COL].astype(str)
mask_exclude = df[Config.ORG_COL].str.strip().str.lower().isin(Config.EXCLUDE_ORGS)
excluded_count = int(mask_exclude.sum())
if excluded_count:
    print(f"Excluding {excluded_count} rows from orgs: {sorted(list(Config.EXCLUDE_ORGS))}")
df = df[~mask_exclude].copy()

# Load org-specific rules (optional)
org_rules = {}
if os.path.exists(Config.RULES_FILE):
    rules_df = load_csv_with_fallbacks(Config.RULES_FILE)
    if 'organisation' in rules_df.columns and 'claim_date_source' in rules_df.columns:
        for _, r in rules_df.iterrows():
            org = str(r['organisation']).strip().lower()
            src = str(r.get('claim_date_source', '')).strip()
            if not src or src.lower() == 'nan':
                continue
            org_rules.setdefault(org, [])
            if src.startswith('css:'):
                payload = src[len('css:'):].strip()
                parts = payload.split('@',1)
                selector = parts[0].strip()
                attr = parts[1].strip() if len(parts) == 2 else 'text'
                org_rules[org].append({'type':'css','selector':selector,'attr':attr})
            elif src.startswith('regex:'):
                pattern = src[len('regex:'):].strip()
                org_rules[org].append({'type':'regex','pattern':pattern})
            else:
                # heuristic: treat as regex unless it looks like a CSS selector
                if '<' in src or ' ' in src or src.startswith('.') or src.startswith('#'):
                    parts = src.split('@',1)
                    selector = parts[0].strip()
                    attr = parts[1].strip() if len(parts) == 2 else 'text'
                    org_rules[org].append({'type':'css','selector':selector,'attr':attr})
                else:
                    org_rules[org].append({'type':'regex','pattern':src})
    else:
        print("Rules file found but missing columns 'organisation' and 'claim_date_source'; skipping rules.")
else:
    print("No rules file found — continuing without per-org CSS/regex rules.")

all_orgs = df[Config.ORG_COL].dropna().unique().tolist()
print(f"Organizations discovered after filtering: {len(all_orgs)}")

# -----------------------
# Date parsing and normalizing - ORIGINAL
# -----------------------
def parse_partial_date(raw: str):
    """Return (iso_str, granularity) where granularity in {'day','month','year'}"""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    s = re.sub(r'\s+', ' ', s)

    # Try UK style first, then fallback to US
    for dayfirst in (True, False):
        try:
            dt = parser.parse(s, fuzzy=True, dayfirst=dayfirst)
            return dt.strftime("%Y-%m-%d"), "day"
        except Exception:
            pass

    # month-year
    m = re.search(r'\b([A-Za-z]{3,9})\s+(\d{4})\b', s)
    if m:
        month_name, year = m.groups()
        try:
            dt = parser.parse(f"01 {month_name} {year}", fuzzy=True, dayfirst=True)
            return dt.strftime("%Y-%m"), "month"
        except Exception:
            pass

    # year-only
    if re.fullmatch(r'\d{4}', s):
        return s, "year"

    return None, None

def parse_article_date(raw):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    for dayfirst in (True, False):  # UK first, then US
        try:
            dt = parser.parse(s, fuzzy=True, dayfirst=dayfirst)
            return date(dt.year, dt.month, dt.day)
        except Exception:
            continue
    return None

def normalize_for_compare(iso_str, granularity):
    if iso_str is None:
        return None
    try:
        if granularity == "day":
            y, m, d = map(int, iso_str.split('-'))
            return date(y,m,d)
        elif granularity == "month":
            y, m = map(int, iso_str.split('-'))
            if Config.PARTIAL_DATE_NORMALIZATION == "start":
                return date(y,m,1)
            else:
                last = monthrange(y,m)[1]
                return date(y,m,last)
        elif granularity == "year":
            y = int(iso_str)
            if Config.PARTIAL_DATE_NORMALIZATION == "start":
                return date(y,1,1)
            else:
                return date(y,12,31)
    except Exception:
        return None

# -----------------------
# Candidate extraction helpers - ORIGINAL
# -----------------------
SOURCE_PRIORITY = {"jsonld_claim":0, "rule":1, "link":2, "text":3, "url":4, "fallback":5}
GRAN_WEIGHT = {"day":0, "month":1, "year":2}

DATE_PATTERNS_TEXT = [
    r'\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b',
    r'\b([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})\b',
    r'\b(\d{4}-\d{2}-\d{2})\b',
    r'\b(\d{1,2}/\d{1,2}/\d{4})\b',
    r'\b(\d{1,2}\.\d{1,2}\.\d{4})\b',
    r'\b([A-Za-z]{3,9}\s+\d{4})\b',
    r'\b((?:19|20)\d{2})\b'
]

CLAIM_TEXT_WINDOWS = [
    r'(?:posted|shared|tweeted|wrote|said|claimed|asserted)[^\.]{0,120}?\bon\b[^\.]{0,60}?\b',
    r'(?:went\s+viral|began\s+circulating|started\s+circulating|spread|spreading)[^\.]{0,120}?\b(in|since|around)\b[^\.]{0,60}?\b',
    r'\b(in|since|around)\s+[A-Za-z]{3,9}\s+\d{4}[^\.]{0,160}?\b(claim|users|posts|shared|tweeted|viral|circulat|spread)\b',
    r'\bclaim[^\.]{0,180}?\b'
]

SOCIAL_DOMAINS = [
    'twitter.com','x.com','facebook.com','instagram.com','youtube.com',
    'tiktok.com','linkedin.com','reddit.com','whatsapp.com','telegram.org','snapchat.com'
]

def is_social_url(href):
    try:
        host = urlparse(href).netloc.lower()
        return any(d in host for d in SOCIAL_DOMAINS)
    except Exception:
        return False

def extract_from_jsonld_claim(soup):
    results = []
    for script in soup.find_all('script', type='application/ld+json'):
        txt = script.string
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            try:
                data = json.loads("[" + txt.replace("}{","},{") + "]")
            except Exception:
                continue
        def visit(node):
            if isinstance(node, dict):
                t = str(node.get('@type') or node.get('type') or '').lower()
                if t == 'claimreview':
                    item = node.get('itemReviewed') or node.get('claimReviewed') or {}
                    if isinstance(item, dict):
                        for key in ('claimDate','dateClaimed','datePublished','dateCreated','uploadDate'):
                            if key in item:
                                iso, gran = parse_partial_date(item.get(key))
                                if iso:
                                    results.append(("jsonld_claim", iso, gran, f"ClaimReview.itemReviewed.{key}"))
                                    return
                for v in node.values():
                    visit(v)
            elif isinstance(node, list):
                for v in node:
                    visit(v)
        visit(data)
    return results

def apply_org_rules(soup, html, org):
    res = []
    if not org:
        return res
    key = org.strip().lower()
    rules = org_rules.get(key, [])
    text = soup.get_text(" ", strip=True)
    for rule in rules:
        if rule['type'] == 'css':
            try:
                sel = rule['selector']
                attr = rule.get('attr','text')
                el = soup.select_one(sel)
                if el:
                    raw = el.get(attr) if attr!='text' else el.get_text(" ", strip=True)
                    iso, gran = parse_partial_date(raw)
                    if iso:
                        res.append(("rule", iso, gran, f"css:{sel}@{attr}"))
            except Exception:
                continue
        else:
            try:
                patt = rule['pattern']
                m = re.search(patt, html, re.IGNORECASE)
                if m:
                    raw = m.group(1) if m.groups() else m.group(0)
                    iso, gran = parse_partial_date(raw)
                    if iso:
                        res.append(("rule", iso, gran, f"regex:{patt[:120]}"))
            except re.error:
                continue
    return res

def extract_dates_from_text(text):
    found = []
    for pat in DATE_PATTERNS_TEXT:
        for m in re.finditer(pat, text, re.IGNORECASE):
            found.append(m.group(1) if m.groups() else m.group(0))
    return found

def extract_from_links(soup):
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('javascript:','mailto:','tel:','#')):
            continue
        texts = [a.get_text(" ", strip=True)]
        parent = a.parent
        if parent:
            texts.append(parent.get_text(" ", strip=True))
            prev = parent.previous_sibling
            for _ in range(2):
                if hasattr(prev,'get_text'):
                    texts.append(prev.get_text(" ", strip=True))
                prev = getattr(prev,'previous_sibling', None)
            nxt = parent.next_sibling
            for _ in range(2):
                if hasattr(nxt,'get_text'):
                    texts.append(nxt.get_text(" ", strip=True))
                nxt = getattr(nxt,'next_sibling', None)
        context = " ".join([t for t in texts if t])
        likely = is_social_url(href) or any(re.search(c, context, re.IGNORECASE) for c in CLAIM_TEXT_WINDOWS) or any(re.search(c, context, re.IGNORECASE) for c in [r'claim', r'posted', r'shared', r'tweeted'])
        if likely:
            for raw in extract_dates_from_text(context):
                iso, gran = parse_partial_date(raw)
                if iso:
                    results.append(("link", iso, gran, context[:200]))
            parsed = urlparse(href)
            for part in (parsed.path, parsed.query):
                for raw in extract_dates_from_text(part):
                    iso, gran = parse_partial_date(raw)
                    if iso:
                        results.append(("url", iso, gran, href[:300]))
    return results

def extract_from_text_blocks(soup):
    results = []
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all(['p','li','blockquote'])]
    if not paragraphs:
        paragraphs = [soup.get_text(" ", strip=True)]
    for block in paragraphs:
        for win_pat in CLAIM_TEXT_WINDOWS:
            for m in re.finditer(win_pat, block, re.IGNORECASE):
                start = max(0, m.start()-160)
                end = min(len(block), m.end()+160)
                snippet = block[start:end]
                for raw in extract_dates_from_text(snippet):
                    iso, gran = parse_partial_date(raw)
                    if iso:
                        results.append(("text", iso, gran, snippet[:300]))
    return results

FALLBACK_PATTERNS = [
    r'(?:Claimed on|Stated on|First posted on|Posted on)\s*[:\-]?\s*([A-Za-z0-9, \-/]+?\d{4})',
    r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
    r'([A-Za-z]+\s+\d{1,2},\s*\d{4})',
    r'(\d{4}-\d{2}-\d{2})',
    r'(\d{1,2}/\d{1,2}/\d{2,4})',
    r'([A-Za-z]{3,9}\s+\d{4})',
    r'((?:19|20)\d{2})'
]

def fallback_search(soup):
    text = soup.get_text(" ", strip=True)
    res = []
    for pat in FALLBACK_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = m.group(1) if m.groups() else m.group(0)
            iso, gran = parse_partial_date(raw)
            if iso:
                res.append(("fallback", iso, gran, raw[:200]))
    return res

def extract_claim_candidates(html, url, org=None):
    soup = BeautifulSoup(html, "lxml")
    candidates = []
    candidates.extend(extract_from_jsonld_claim(soup))
    candidates.extend(apply_org_rules(soup, html, org))
    candidates.extend(extract_from_links(soup))
    candidates.extend(extract_from_text_blocks(soup))
    candidates.extend(fallback_search(soup))
    # dedupe preserving discovery order
    seen = set(); unique = []
    for s, iso, gran, ctx in candidates:
        k = (s, iso, gran)
        if k not in seen:
            seen.add(k); unique.append((s, iso, gran, ctx))
    return unique

# -----------------------
# Validation & recency helpers - ORIGINAL
# -----------------------
def is_candidate_basic_valid(iso, gran, article_dt):
    """Basic checks: candidate parseable and <= article date (if article_dt present)."""
    if not iso or not gran:
        return False
    cand_dt = normalize_for_compare(iso, gran)
    if cand_dt is None:
        return False
    if article_dt:
        # must not be after article date
        if cand_dt > article_dt:
            return False
        # enforce same-day/strictness flags
        if Config.ENFORCE_DIFFERENT_FROM_ARTICLE and not Config.ALLOW_SAME_DAY_AS_ARTICLE:
            if gran == "day" and cand_dt == article_dt:
                return False
            if gran == "month" and cand_dt.year == article_dt.year and cand_dt.month == article_dt.month:
                return False
            if gran == "year" and cand_dt.year == article_dt.year:
                return False
        if Config.REQUIRE_BEFORE_ARTICLE:
            return cand_dt < article_dt if not Config.ALLOW_SAME_DAY_AS_ARTICLE else cand_dt <= article_dt
        else:
            return cand_dt <= article_dt
    else:
        # no article date -> basic valid if parseable
        return True

def is_candidate_recent(iso, gran, article_dt):
    """Recency check: within MAX_DAYS_BEFORE_PUB relative to article_dt, else relative to today."""
    cand_dt = normalize_for_compare(iso, gran)
    if cand_dt is None:
        return False
    if article_dt:
        diff = (article_dt - cand_dt).days
        return (0 <= diff <= Config.MAX_DAYS_BEFORE_PUB)
    else:
        today = date.today()
        diff = (today - cand_dt).days
        return (0 <= diff <= Config.MAX_DAYS_FROM_NOW)

def choose_best_candidate_inorder(all_candidates, article_dt, org):
    """
    New selection policy (pick the most recent valid candidate before publication,
    prefer link/url when tie, prefer rule/jsonld if tie afterwards).
    Returns: chosen_candidate_tuple_or_None, list_of_valid_candidates
    """
    basic_valid = []
    for s, iso, gran, ctx in all_candidates:
        if not iso:
            continue
        if gran == "month" and not Config.ACCEPT_MONTH_YEAR:
            continue
        if gran == "year" and not Config.ACCEPT_YEAR_ONLY:
            continue
        if is_candidate_basic_valid(iso, gran, article_dt):
            cand_dt = normalize_for_compare(iso, gran)
            basic_valid.append((s, iso, gran, ctx, cand_dt))

    if not basic_valid:
        return None, []

    # Prefer candidates that are recent (within MAX_DAYS_BEFORE_PUB relative to article)
    recent = [c for c in basic_valid if is_candidate_recent(c[1], c[2], article_dt)]

    def sort_key(c):
        # c: (source, iso, gran, ctx, cand_dt)
        source, iso, gran, ctx, cand_dt = c
        # primary: most recent -> larger cand_dt
        primary = cand_dt.toordinal() if cand_dt else -10**9
        # secondary: prefer link/url (lower SOURCE_PRIORITY value is better)
        sp = SOURCE_PRIORITY.get(source, 9)
        # tertiary: prefer finer granularity (day < month < year)
        gw = GRAN_WEIGHT.get(gran, 9)
        # lower sp and lower gw are better; we sort by (-primary, sp, gw)
        return (-primary, sp, gw)

    if recent:
        recent_sorted = sorted(recent, key=sort_key)
        chosen = recent_sorted[0]
        # return as (s, iso, gran, ctx)
        return (chosen[0], chosen[1], chosen[2], chosen[3]), [(c[0], c[1], c[2], c[3]) for c in recent_sorted]

    # If none recent, pick the most recent among basic_valid and mark relaxed
    basic_sorted = sorted(basic_valid, key=sort_key)
    chosen = basic_sorted[0]
    return (chosen[0], chosen[1], chosen[2], chosen[3]), [(c[0], c[1], c[2], c[3]) for c in basic_sorted]

# -----------------------
# Cache mechanism - ORIGINAL
# -----------------------
@lru_cache(maxsize=Config.CACHE_SIZE)
async def cached_fetch(session, url):
    """Cache fetched HTML content to avoid redundant requests"""
    return await fetch_with_retry(session, url)

# -----------------------
# Async fetch + row processing - ORIGINAL
# -----------------------
ua = UserAgent()
limiter = aiolimiter.AsyncLimiter(Config.RATE_LIMIT_REQUESTS, Config.RATE_LIMIT_PERIOD)

async def fetch_with_retry(session, url, retries=Config.RETRY_ATTEMPTS):
    last_err = None
    for attempt in range(retries):
        try:
            async with limiter:
                async with session.get(url, headers={"User-Agent": ua.random}, timeout=Config.REQUEST_TIMEOUT, ssl=False) as resp:
                    if resp.status == 200:
                        return await resp.text(), None
                    elif resp.status == 404:
                        return None, "HTTP_404"
                    last_err = f"HTTP_{resp.status}"
        except Exception as e:
            last_err = f"NETWORK_ERROR:{str(e)}"
        await asyncio.sleep(1.0 * (attempt + 1))
    return None, last_err or "MAX_RETRIES_EXCEEDED"

async def process_row(session, semaphore, row, pbar, idx):
    """ORIGINAL PROCESS_ROW FUNCTION - MAX SUCCESS RATE"""
    async with semaphore:
        url = row[Config.URL_COL]
        org_raw = str(row.get(Config.ORG_COL, "") or "").strip()
        org = org_raw.lower()
        article_raw = row.get(Config.ARTICLE_DATE_COL, "")

        # Parse article date early for all paths
        article_dt = parse_article_date(article_raw)

        # Forced override: claim_date == published_date for certain orgs
        if org in Config.FORCE_CLAIM_EQ_PUB_ORGS:
            cand_records = []
            if article_dt:
                iso = article_dt.isoformat()
                pbar.update(1); pbar.set_postfix_str(f"override {iso}")
                return {
                    **row.to_dict(),
                    "claim_iso": iso,
                    "claim_granularity": "day",
                    "claim_source": "override_same_as_published",
                    "status": "SUCCESS_OVERRIDE",
                    "article_date_clean": article_dt.isoformat(),
                    "candidates": 0,
                    "claim_date_candidates": json.dumps(cand_records, default=str),
                    "recency_relaxed": False,
                    "error_category": "N/A",
                    "processed_at": datetime.now().isoformat()
                }
            else:
                pbar.update(1); pbar.set_postfix_str("override_no_pubdate")
                return {
                    **row.to_dict(),
                    "claim_iso": None,
                    "claim_granularity": None,
                    "claim_source": "override_same_as_published",
                    "status": "PUBLISH_DATE_PARSE_FAILED",
                    "article_date_clean": None,
                    "candidates": 0,
                    "claim_date_candidates": json.dumps([], default=str),
                    "recency_relaxed": False,
                    "error_category": "PARSING",
                    "processed_at": datetime.now().isoformat()
                }

        # Normal path: fetch + extract
        html, err = await cached_fetch(session, url)
        if html is None:
            pbar.update(1); pbar.set_postfix_str(f"❌ fetch {err}")
            return {**row.to_dict(),
                    "claim_iso": None,
                    "claim_granularity": None,
                    "claim_source": "fetch_failed",
                    "status": err,
                    "article_date_clean": article_dt.isoformat() if article_dt else None,
                    "candidates": 0,
                    "claim_date_candidates": json.dumps([]),
                    "recency_relaxed": False,
                    "error_category": "NETWORK",
                    "processed_at": datetime.now().isoformat()}

        all_cands = extract_claim_candidates(html, url, org)

        # Build candidate list with validity + recent flags (for auditing)
        cand_records = []
        for s, iso, gran, ctx in all_cands:
            valid_flag = is_candidate_basic_valid(iso, gran, article_dt)
            recent_flag = is_candidate_recent(iso, gran, article_dt) if valid_flag else False
            cand_records.append({"source": s, "iso": iso, "granularity": gran, "ctx": ctx, "valid": valid_flag, "recent": recent_flag})

        chosen, valid_list = choose_best_candidate_inorder(all_cands, article_dt, org)

        await asyncio.sleep(random.uniform(Config.DELAY_MIN, Config.DELAY_MAX))

        if chosen:
            s, iso, gran, ctx = chosen
            recency_relaxed = not is_candidate_recent(iso, gran, article_dt)
            pbar.update(1); pbar.set_postfix_str(f" {iso} ({gran})")
            return {
                **row.to_dict(),
                "claim_iso": iso,
                "claim_granularity": gran,
                "claim_source": s,
                "status": "SUCCESS_RELAXED" if recency_relaxed else "SUCCESS",
                "article_date_clean": article_dt.isoformat() if article_dt else None,
                "candidates": len(valid_list),
                "claim_date_candidates": json.dumps(cand_records, default=str),
                "recency_relaxed": recency_relaxed,
                "error_category": "N/A",
                "processed_at": datetime.now().isoformat()
            }
        else:
            pbar.update(1); pbar.set_postfix_str(" no_valid")
            return {
                **row.to_dict(),
                "claim_iso": None,
                "claim_granularity": None,
                "claim_source": "none",
                "status": "NO_VALID_DATES",
                "article_date_clean": article_dt.isoformat() if article_dt else None,
                "candidates": 0,
                "claim_date_candidates": json.dumps(cand_records, default=str),
                "recency_relaxed": False,
                "error_category": "PARSING",
                "processed_at": datetime.now().isoformat()
            }

# -----------------------
# MODIFIED MAIN FUNCTION WITH CHECKPOINTING
# -----------------------
async def main_with_checkpoints():
    # Load previous progress
    checkpoint = load_checkpoint()
    partial_results = load_partial_results()
    
    if checkpoint:
        start_index = checkpoint['processed_count']
        total_urls = checkpoint['total_urls']
        all_results = checkpoint['results']
    else:
        start_index = 0
        total_urls = len(df)
        all_results = partial_results
    
    print(f"🎯 Processing URLs {start_index + 1} to {total_urls} of {total_urls}")
    
    connector = aiohttp.TCPConnector(limit=Config.MAX_CONCURRENT_REQUESTS, ssl=False)
    semaphore = asyncio.Semaphore(Config.CONCURRENCY)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        with tqdm(total=total_urls, initial=start_index, desc="Extracting claim dates") as pbar:
            tasks = []
            
            # Create tasks for unprocessed URLs only
            for idx in range(start_index, total_urls):
                row = df.iloc[idx]
                task = asyncio.create_task(process_row(session, semaphore, row, pbar, idx))
                tasks.append(task)
                
                # Save checkpoint periodically
                if (idx - start_index) % Config.CHECKPOINT_INTERVAL == 0 and idx > start_index:
                    # Wait for current batch to complete
                    batch_results = []
                    for task in asyncio.as_completed(tasks):
                        result = await task
                        batch_results.append(result)
                        all_results.append(result)
                    
                    save_checkpoint(idx + 1, all_results, total_urls)
                    save_partial_results(all_results)
                    tasks = []  # Reset tasks for next batch
            
            # Process remaining tasks
            if tasks:
                for task in asyncio.as_completed(tasks):
                    result = await task
                    all_results.append(result)
            
            # Final save
            save_checkpoint(total_urls, all_results, total_urls)
            save_partial_results(all_results)

    # Final results
    results_df = pd.DataFrame(all_results)
    success_count = results_df["claim_iso"].notna().sum()
    
    print(f"\n✅ Completed! Success: {success_count}/{total_urls} ({success_count/total_urls*100:.1f}%)")
    
    # Save final results
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_output = f"claim_date_verification_COMPLETE_{ts}.csv"
    results_df.to_csv(final_output, index=False)
    
    # Cleanup checkpoint files
    if os.path.exists(Config.CHECKPOINT_FILE):
        os.remove(Config.CHECKPOINT_FILE)
    if os.path.exists(Config.RESULTS_FILE):
        os.remove(Config.RESULTS_FILE)
    
    print(f" Final results saved: {final_output}")
    return results_df

# -----------------------
# EXECUTION WITH CHECKPOINT SUPPORT
# -----------------------
def restart_processing():
    """Use this function to restart after disconnection"""
    print(" Attempting to restart from last checkpoint...")
    return asyncio.run(main_with_checkpoints())

# First time execution
if __name__ == "__main__":
    # Check if we need to resume or start fresh
    if os.path.exists(Config.CHECKPOINT_FILE):
        print("📋 Checkpoint found! Resuming...")
        results_df = restart_processing()
    else:
        print("🚀 Starting fresh processing with MAX SUCCESS RATE...")
        results_df = asyncio.run(main_with_checkpoints())
