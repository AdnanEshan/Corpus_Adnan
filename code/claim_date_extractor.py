# %% [markdown]

!pip install aiohttp lxml tqdm pandas beautifulsoup4 fake-useragent nest_asyncio -q


import pandas as pd
import aiohttp
import asyncio
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime
from tqdm.notebook import tqdm
import logging
import random
from urllib.parse import urlparse
import nest_asyncio
from aiohttp.client_exceptions import (
    ClientPayloadError,
    ContentTypeError,
    ClientConnectorError,
    ServerTimeoutError,
)

# Enable nested asyncio in Colab
nest_asyncio.apply()

# %%
# @title Configuration
class Config:
    MAX_CONCURRENT_REQUESTS = 30   # Reduced for stability
    REQUEST_TIMEOUT = 20           # seconds
    RETRY_ATTEMPTS = 3
    BACKOFF_BASE = 2               # exponential backoff base
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
    ]
    CSV_PATH = '/content/FACTors (2).csv'  # Default Colab file location

# %%
# @title Robust CSV Loader
def load_urls():
    """Load URLs with error handling for malformed CSV and flexible encodings."""
    encodings_try = ['utf-8', 'utf-8-sig', 'ISO-8859-1', 'cp1252']
    last_err = None
    df = None
    for enc in encodings_try:
        try:
            df = pd.read_csv(
                Config.CSV_PATH,
                encoding=enc,
                on_bad_lines='skip',   # skip malformed rows
                engine='python'        # tolerant parser
            )
            break
        except Exception as e:
            last_err = e
            continue

    if df is None:
        print(f"❌ CSV Error: {last_err}")
        return []

    # Normalize columns
    df.columns = df.columns.str.strip().str.lower()
    # Find a likely URL column
    url_col_candidates = [c for c in df.columns if c in ('url','link','source','source_url','article_url')]
    if not url_col_candidates:
        print("❌ Error: CSV must contain a URL-like column (url/link/source).")
        return []

    url_col = url_col_candidates[0]
    urls = (
        df[url_col]
        .dropna()
        .astype(str)
        .str.strip()
        .replace({'': None})
        .dropna()
        .unique()
        .tolist()
    )
    print(f"✅ Loaded {len(urls)} URLs from {Config.CSV_PATH} (column: '{url_col}')")
    return urls

# %%
# @title Date Extraction Functions (Upgraded)
MONTHS_REGEX = r'(January|February|March|April|May|June|July|August|September|October|November|December)'
MONTHS_ABBR_REGEX = r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)'

def clean_date(raw):
    """Normalize various date strings to YYYY-MM-DD where possible."""
    if not raw or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()

    # Common ISO tweaks (handle Z / timezone)
    try:
        iso_candidate = s.replace('Z', '+00:00')
        # drop milliseconds if present for fromisoformat compatibility
        iso_candidate = re.sub(r'(\.\d+)(?=[Z\+\-])', '', iso_candidate)
        dt = None
        try:
            dt = datetime.fromisoformat(iso_candidate)
        except ValueError:
            pass
        if dt:
            return dt.strftime('%Y-%m-%d')
    except Exception:
        pass

    fmts = [
        '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d',
        '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y',
        '%m/%d/%Y', '%m-%d-%Y', '%m.%d.%Y',
        '%B %d, %Y', '%b %d, %Y', '%d %B %Y', '%d %b %Y',
        '%Y%m%d', '%d-%b-%Y', '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S%z'
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue

    # Numeric fallback like 2024-7-3 or 2024/7/03
    m = re.search(r'\b(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})\b', s)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"

    # Month name patterns: "12 March 2024" or "March 12, 2024"
    m2 = re.search(rf'\b(\d{{1,2}})\s+{MONTHS_REGEX}\s+(\d{{4}})\b', s)
    if m2:
        try:
            return datetime.strptime(m2.group(0), "%d %B %Y").strftime('%Y-%m-%d')
        except ValueError:
            pass
    m3 = re.search(rf'\b{MONTHS_REGEX}\s+\d{{1,2}},\s+\d{{4}}\b', s)
    if m3:
        try:
            return datetime.strptime(m3.group(0), "%B %d, %Y").strftime('%Y-%m-%d')
        except ValueError:
            pass
    # Abbreviated month name patterns
    m4 = re.search(rf'\b(\d{{1,2}})\s+{MONTHS_ABBR_REGEX}\.?\s+(\d{{4}})\b', s)
    if m4:
        try:
            return datetime.strptime(m4.group(0).replace('.', ''), "%d %b %Y").strftime('%Y-%m-%d')
        except ValueError:
            pass
    m5 = re.search(rf'\b{MONTHS_ABBR_REGEX}\.?\s+\d{{1,2}},\s+\d{{4}}\b', s)
    if m5:
        try:
            return datetime.strptime(m5.group(0).replace('.', ''), "%b %d, %Y").strftime('%Y-%m-%d')
        except ValueError:
            pass

    return None


def extract_date_from_html(html, url):
    """
    Extract date from HTML using a cascade:
    1) Meta/time tags
    2) JSON-LD (<script type="application/ld+json">)
    3) Visible text
    4) JavaScript inline variables
    Returns (date, source) where source is one of 'meta','jsonld','text','js' or 'none'.
    """
    try:
        soup = BeautifulSoup(html, 'lxml')
        domain = (urlparse(url).netloc or '').lower()

        # ---- 1) Meta/time tags ----
        selectors = [
            ('meta[property="article:published_time"]', 'content'),
            ('meta[name="article:published_time"]', 'content'),
            ('meta[name="datePublished"]', 'content'),
            ('meta[itemprop="datePublished"]', 'content'),
            ('meta[name="pubdate"]', 'content'),
            ('meta[name="publish-date"]', 'content'),
            ('meta[name="DC.date.issued"]', 'content'),
            ('meta[name="DC.date.created"]', 'content'),
            ('meta[property="og:updated_time"]', 'content'),
            ('time[datetime]', 'datetime'),
            ('time', None),
        ]
        # Domain-specific tweaks
        if 'afp.com' in domain or 'factcheck.afp.com' in domain:
            selectors = [
                ('time[datetime]', 'datetime'),
                ('meta[property="article:published_time"]', 'content'),
                ('span.article-date', None),
            ] + selectors

        for css, attr in selectors:
            el = soup.select_one(css)
            if el:
                raw = el.get(attr) if attr else el.get_text(" ", strip=True)
                dt = clean_date(raw)
                if dt:
                    return dt, 'meta'

        # ---- 2) JSON-LD blocks ----
        def _scan_jsonld(obj):
            if isinstance(obj, dict):
                # common keys
                for k in ("datePublished", "dateCreated", "uploadDate", "dateModified"):
                    if k in obj:
                        dt = clean_date(obj.get(k))
                        if dt:
                            return dt
                #@graph nested structures
                if "@graph" in obj and isinstance(obj["@graph"], list):
                    for item in obj["@graph"]:
                        dt = _scan_jsonld(item)
                        if dt:
                            return dt
            elif isinstance(obj, list):
                for item in obj:
                    dt = _scan_jsonld(item)
                    if dt:
                        return dt
            return None

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                # Some sites embed multiple JSON objects concatenated; attempt safe parse
                txt = script.string
                if not txt:
                    continue
                data = json.loads(txt)
                dt = _scan_jsonld(data)
                if dt:
                    return dt, 'jsonld'
            except (json.JSONDecodeError, TypeError):
                continue

        # ---- 3) Visible text fallback ----
        text = soup.get_text(" ", strip=True)
        # Try multiple patterns, most specific first
        patterns = [
            rf'\b{MONTHS_REGEX}\s+\d{{1,2}},\s+\d{{4}}\b',
            rf'\b\d{{1,2}}\s+{MONTHS_REGEX}\s+\d{{4}}\b',
            r'\b\d{4}-\d{1,2}-\d{1,2}\b',
            r'\b\d{1,2}/\d{1,2}/\d{4}\b',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                dt = clean_date(m.group(0))
                if dt:
                    return dt, 'text'

        # ---- 4) JavaScript inline variables ----
        for script in soup.find_all("script"):
            s = script.string
            if not s:
                continue
            for pat in patterns:
                m = re.search(pat, s)
                if m:
                    dt = clean_date(m.group(0))
                    if dt:
                        return dt, 'js'

        return None, 'none'

    except Exception as e:
        return None, f'error: {e}'

# %%
# @title Async Processing Core (robust)
async def fetch_url(session, url, semaphore):
    """Fetch a single URL with retries, robust decoding, and consistent result schema."""
    async with semaphore:
        for attempt in range(Config.RETRY_ATTEMPTS):
            try:
                headers = {'User-Agent': random.choice(Config.USER_AGENTS)}
                timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    status = resp.status
                    if status != 200:
                        # Non-OK result: do not try to parse HTML
                        return {
                            'url': url,
                            'date': None,
                            'source': 'none',
                            'status': f'HTTP {status}',
                            'error': None if status in (404, 410) else f'HTTP {status}'
                        }

                    # Robust decode: read bytes, then decode with fallback
                    raw = await resp.read()
                    charset = resp.charset or 'utf-8'
                    try:
                        html = raw.decode(charset, errors='replace')
                    except Exception:
                        # last resort
                        html = raw.decode('utf-8', errors='replace')

                    date, source = extract_date_from_html(html, url)
                    return {
                        'url': url,
                        'date': date,
                        'source': source,
                        'status': 'success' if date else 'date not found',
                        'error': None if date else 'Date parsing failed'
                    }

            except (ServerTimeoutError, ClientPayloadError, ContentTypeError, ClientConnectorError) as e:
                err = type(e).__name__
            except asyncio.TimeoutError:
                err = "Timeout"
            except Exception as e:
                err = str(e)

            # If last attempt, give up
            if attempt == Config.RETRY_ATTEMPTS - 1:
                return {
                    'url': url,
                    'date': None,
                    'source': 'none',
                    'status': 'error',
                    'error': err
                }

            # Exponential backoff before retrying
            await asyncio.sleep(Config.BACKOFF_BASE ** attempt)

async def process_all_urls(urls):
    """Process all URLs concurrently with a connection limit."""
    connector = aiohttp.TCPConnector(limit=Config.MAX_CONCURRENT_REQUESTS, force_close=True)
    semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_url(session, url, semaphore) for url in urls]
        results = []
        for fut in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Processing URLs"):
            results.append(await fut)
        return results

# %%
# @title Main Execution
def run_analysis():
    print("🚀 Starting fact-check analysis...")

    # 1) Load URLs
    urls = load_urls()
    if not urls:
        return

    # 2) Process URLs
    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(process_all_urls(urls))

    # 3) Save results
    df = pd.DataFrame(results, columns=['url','date','source','status','error'])
    output_file = f"factcheck_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(output_file, index=False)

    # 4) Display summary
    success = (df['status'] == 'success').sum()
    total = len(df)
    print(f"\n📊 Results saved to {output_file}")
    print(f"✅ Success: {success}/{total} ({success/total:.1%})")
    print(f"❌ Failed or missing: {total - success}/{total}")

    # Show samples
    if success:
        print("\nSample successful results:")
        print(df[df['status'] == 'success'].head(5))
    if success < total:
        print("\nSample non-success results:")
        print(df[df['status'] != 'success'].head(5))


run_analysis()
