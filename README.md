**Fact-Checking Date Extractor**

This project extracts claim dates from a dataset (FACTors.csv) by fetching and parsing associated URLs.
It uses asynchronous web scraping for efficiency and outputs a cleaned dataset with extracted claim dates.

**Features**

Reads and processes FACTors.csv.

Uses asyncio + aiohttp for fast parallel URL fetching.

Parses HTML with BeautifulSoup and lxml.

Extracts claim dates from fact-check articles.

Outputs results to a CSV file.

**Requirements**

Install dependencies:
```bash
pip install pandas aiohttp beautifulsoup4 lxml fake-useragent tqdm nest_asyncio
```
**Dataset**

The project expects a dataset named FACTors.csv with at least a column containing fact-check URLs.
You can modify the script if your dataset has a different structure.

**Usage**

1. Place your dataset (FACTors.csv) in the project folder.

2. Run the script:
```bash
python claim_date_extractor.py
```
3. Output:

A new CSV file will be generated (e.g., FACTors_with_dates.csv) containing the original data and an extra column with the extracted claim date.

**File Structure**
```
├── claim_date_extractor.py    # Main script
├── FACTors.csv                # Input dataset (place your file here)
├── README.md                  # Project documentation
└── output.csv                 # Generated output with claim dates
```
**Example Output**
ID	URL	Claim Date
1	https://example.com/fact-check/123	2024-05-21
2	https://example.com/fact-check/456	2023-12-11
Notes

If a claim date cannot be found, the script will leave the field blank.

Websites with anti-bot measures might block the scraper; in that case, adjust headers or delays in the script.

**License**

This project is licensed under the MIT License — feel free to use and modify.
