!pip install aiohttp lxml tqdm pandas beautifulsoup4 fake-useragent nest_asyncio python-dateutil -q

import pandas as pd
import aiohttp
import asyncio
from bs4 import BeautifulSoup
import re
import random
import nest_asyncio
import logging
from fake_useragent import UserAgent
from urllib.parse import urlparse
from dateutil import parser
from tqdm.notebook import tqdm
import json
from datetime import datetime

nest_asyncio.apply()
logging.basicConfig(level=logging.WARNING)

# -------------------------
# Configuration
# -------------------------
class Config:
    MAX_CONCURRENT_REQUESTS = 10
    REQUEST_TIMEOUT = 30
    RETRY_ATTEMPTS = 2
    DELAY_MIN = 1.0
    DELAY_MAX = 2.5

# -------------------------
# Load datasets
# -------------------------
factors_file = "/content/FACTors.csv"
rules_file = "/content/top_10_urls_per_organisation - top_10_urls_per_organisation.csv (2).csv"

df = pd.read_csv(factors_file)
rules_df = pd.read_csv(rules_file)

# Build rules dict: organisation -> list of regex patterns
org_rules = {}
for _, row in rules_df.iterrows():
    org = str(row["organisation"]).strip()
    pattern = str(row.get("claim_date_source", "")).strip()
    if org and pattern and pattern.lower() != "nan":
        org_rules.setdefault(org, []).append(pattern)

# -------------------------
# Enhanced Claim date cleaner with UK-first parsing
# -------------------------
def clean_date(raw):
    try:
        # Try UK format first (day/month/year)
        dt = parser.parse(str(raw), fuzzy=True, dayfirst=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        try:
            # Fallback to US format (month/day/year)
            dt = parser.parse(str(raw), fuzzy=True, dayfirst=False)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

# -------------------------
# Enhanced regex patterns (ordered by specificity)
# -------------------------
FALLBACK_REGEXES = [
    # Most specific patterns first
    r"Claimed on\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{4})",
    r"Posted on\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{4})",
    r"Shared on\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{4})",

    # Structured data patterns
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'"dateCreated"\s*:\s*"([^"]+)"',
    r'datePublished["\']\s*:\s*["\']([^"\']+)',

    # Date patterns (more specific first)
    r"(\d{1,2}\s+\w+\s+\d{4})",  # e.g., 12 March 2021
    r"(\w+\s+\d{1,2},\s*\d{4})",  # e.g., March 12, 2021
    r"(\d{4}-\d{2}-\d{2})",  # ISO format
    r"(\d{2}/\d{2}/\d{4})",  # MM/DD/YYYY or DD/MM/YYYY
]

# -------------------------
# Enhanced extraction function
# -------------------------
def extract_claim_date(html, org, url):
    if not html:
        return None, "no_html"

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    # 1. Try to extract from JSON-LD structured data first
    claim_date = extract_from_structured_data(soup)
    if claim_date:
        return claim_date, "structured_data"

    # 2. Try domain-specific extraction
    domain = urlparse(url).netloc
    claim_date = extract_domain_specific(soup, domain, org)
    if claim_date:
        return claim_date, "domain_specific"

    # 3. Try org-specific rules
    if org in org_rules:
        for pattern in org_rules[org]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                raw_date = match.group(1) if match.groups() else match.group(0)
                cleaned = clean_date(raw_date)
                if cleaned:
                    return cleaned, "org_rule"

    # 4. Try fallback regexes (ordered by specificity)
    for pattern in FALLBACK_REGEXES:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]  # Get first group if it's a tuple
            cleaned = clean_date(match)
            if cleaned:
                return cleaned, "fallback_regex"

    return None, "not_found"

def extract_from_structured_data(soup):
    """Extract from JSON-LD and other structured data, handling arrays"""
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)

            # Handle arrays of JSON-LD objects
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get('@type') == 'ClaimReview':
                        claim = item.get('itemReviewed', {})
                        if isinstance(claim, dict) and claim.get('@type') == 'Claim':
                            date_published = claim.get('datePublished')
                            if date_published:
                                return clean_date(date_published)

            # Handle single JSON-LD object
            elif isinstance(data, dict) and data.get('@type') == 'ClaimReview':
                claim = data.get('itemReviewed', {})
                if isinstance(claim, dict) and claim.get('@type') == 'Claim':
                    date_published = claim.get('datePublished')
                    if date_published:
                        return clean_date(date_published)

        except:
            continue
    return None

def extract_domain_specific(soup, domain, org):
    """Domain-specific extraction logic for all major fact-checking organizations"""
    domain = domain.lower()

    # AAP specific extraction
    if 'aap.com.au' in domain:
        for script in soup.find_all('script'):
            if script.string and 'datePublished' in script.string:
                match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string)
                if match:
                    return clean_date(match.group(1))

    # Snopes specific extraction
    elif 'snopes.com' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # PolitiFact specific extraction
    elif 'politifact.com' in domain:
        desc_span = soup.find('span', class_='m-statement__desc')
        if desc_span:
            match = re.search(r'(\w+\s+\d{1,2},\s*\d{4})', desc_span.get_text())
            if match:
                return clean_date(match.group(1))

    # FullFact specific extraction
    elif 'fullfact.org' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # FactCheck.org specific extraction
    elif 'factcheck.org' in domain:
        date_span = soup.find('span', class_='entry-date')
        if date_span:
            return clean_date(date_span.get_text())

    # AFP Fact Check specific extraction
    elif 'factcheck.afp.com' in domain or 'afp.com' in domain:
        # AFP often uses specific meta tags or structured data
        meta_date = soup.find('meta', {'property': 'article:published_time'})
        if meta_date and meta_date.get('content'):
            return clean_date(meta_date['content'])

    # Africa Check specific extraction
    elif 'africacheck.org' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # BOOM Live specific extraction
    elif 'boomlive.in' in domain:
        date_div = soup.find('div', class_='date')
        if date_div:
            return clean_date(date_div.get_text())

    # Check Your Fact specific extraction
    elif 'checkyourfact.com' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # DFRAC specific extraction
    elif 'dfrac.org' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # Dubawa specific extraction
    elif 'dubawa.org' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # FactCheckNI specific extraction
    elif 'factcheckni.org' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # First Check specific extraction
    elif 'firstcheck.in' in domain:
        date_span = soup.find('span', class_='posted-on')
        if date_span:
            return clean_date(date_span.get_text())

    # Grass Fact Check specific extraction
    elif 'factcheck.ge' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # India Today specific extraction
    elif 'indiatoday.in' in domain:
        date_span = soup.find('span', {'itemprop': 'datePublished'})
        if date_span:
            return clean_date(date_span.get_text())

    # Lead Stories specific extraction
    elif 'leadstories.com' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # MindaNews specific extraction
    elif 'mindanews.com' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # Myth Detector specific extraction
    elif 'mythdetector.ge' in domain or 'mythdetector.com' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # NewsMeter specific extraction
    elif 'newsmeter.in' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # PA Fact Check specific extraction
    elif 'pa.media' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # PressOnePH specific extraction
    elif 'pressone.ph' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # RMIT ABC Fact Check specific extraction
    elif 'abc.net.au' in domain and 'fact-check' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # Science Feedback specific extraction
    elif 'science.feedback.org' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # The Dispatch specific extraction
    elif 'thedispatch.com' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # The Ferret specific extraction
    elif 'theferret.scot' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # TheJournal.ie specific extraction
    elif 'thejournal.ie' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # USA TODAY specific extraction
    elif 'usatoday.com' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # VERA Files specific extraction
    elif 'verafiles.org' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # Vishvas News specific extraction
    elif 'vishvasnews.com' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # WebQoof specific extraction
    elif 'thequint.com' in domain and 'webqoof' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # THIP Media specific extraction
    elif 'thip.media' in domain:
        date_span = soup.find('span', class_='posted-on')
        if date_span:
            return clean_date(date_span.get_text())

    # The Stage Media Liberia specific extraction
    elif 'tsmliberia.com' in domain:
        date_span = soup.find('span', class_='date')
        if date_span:
            return clean_date(date_span.get_text())

    # Wisconsin Watch specific extraction
    elif 'wisconsinwatch.org' in domain:
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

    # For other organizations, try to find common date patterns
    else:
        # Try common meta tags
        meta_date = soup.find('meta', {'property': 'article:published_time'})
        if meta_date and meta_date.get('content'):
            return clean_date(meta_date['content'])

        # Try time tags
        time_tag = soup.find('time', {'datetime': True})
        if time_tag:
            return clean_date(time_tag['datetime'])

        # Try common class names
        for class_name in ['date', 'posted-on', 'entry-date', 'publish-date']:
            date_span = soup.find('span', class_=class_name)
            if date_span:
                cleaned = clean_date(date_span.get_text())
                if cleaned:
                    return cleaned

    return None

# -------------------------
# Enhanced scraper with concurrency
# -------------------------
ua = UserAgent()

async def fetch_with_retry(session, url, retries=Config.RETRY_ATTEMPTS):
    for attempt in range(retries):
        try:
            async with session.get(
                url,
                headers={"User-Agent": ua.random},
                timeout=Config.REQUEST_TIMEOUT,
                ssl=False
            ) as response:
                if response.status == 200:
                    return await response.text()
                elif response.status == 404:
                    return None  # Page doesn't exist
        except Exception as e:
            if attempt == retries - 1:
                logging.warning(f"Failed to fetch {url} after {retries} attempts: {e}")
            await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
    return None

async def process_row(session, semaphore, row, pbar):
    async with semaphore:
        url = row["url"]
        org = str(row.get("organisation", ""))

        html = await fetch_with_retry(session, url)
        claim_date, source = extract_claim_date(html, org, url)

        # Respectful delay
        await asyncio.sleep(random.uniform(Config.DELAY_MIN, Config.DELAY_MAX))

        pbar.update(1)
        if claim_date:
            pbar.set_postfix_str(f"✅ Found: {claim_date}")
        else:
            pbar.set_postfix_str("❌ Not found")

        return {
            **row.to_dict(),
            "claim_date": claim_date,
            "extraction_source": source,
            "processed_at": datetime.now().isoformat()
        }

async def main():
    results = []

    connector = aiohttp.TCPConnector(limit=Config.MAX_CONCURRENT_REQUESTS)
    semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession(connector=connector) as session:
        with tqdm(total=len(df), desc="Extracting claim dates") as pbar:
            tasks = [
                process_row(session, semaphore, row, pbar)
                for _, row in df.iterrows()
            ]

            for future in asyncio.as_completed(tasks):
                try:
                    result = await future
                    results.append(result)
                except Exception as e:
                    logging.error(f"Error processing row: {e}")

    # Create results DataFrame
    results_df = pd.DataFrame(results)

    # Split into success and failure
    success_df = results_df[results_df["claim_date"].notna()]
    failed_df = results_df[results_df["claim_date"].isna()]

    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    success_file = f"succeeded_claim_dates_{timestamp}.csv"
    failed_file = f"failed_claim_dates_{timestamp}.csv"

    success_df.to_csv(success_file, index=False)
    failed_df.to_csv(failed_file, index=False)

    # Print summary
    print(f"\n📊 Extraction Complete:")
    print(f"✅ Successfully extracted: {len(success_df)} claim dates")
    print(f"❌ Failed to extract: {len(failed_df)}")

    if len(success_df) > 0:
        print(f"\n📈 Extraction Sources:")
        source_counts = success_df["extraction_source"].value_counts()
        for source, count in source_counts.items():
            print(f"   {source}: {count} ({count/len(success_df)*100:.1f}%)")

    print(f"\n💾 Results saved to:")
    print(f"   Successful: {success_file}")
    print(f"   Failed: {failed_file}")

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    asyncio.run(main())
