# User Test Transcripts

**Project:** Claim-Date Extraction (FACTors)  
**Testers:** Adnan Rahman Eshan, Sample Users  

## Session 1 — 2025-07-15
**Tester:** User 1  
**Task:** Run the claim-date extractor on `input_urls.csv` sample  
**Observations:**
- User successfully activated Python virtual environment.
- Ran `python code/claim_date_extractor.py --input data/input_urls.csv --output data/sample_results.csv`.
- All sample URLs processed without timeout.
- Extracted dates appear in the `Claim Date` column; the Status column correctly shows "Success" or error type.

**Issues encountered:**
- One URL returned a network timeout, logged as expected.
- User initially missed activating `nest_asyncio` in Jupyter Notebook; guidance provided.

**Comments:**
- Interface and instructions are clear.
- User suggests a short explanation of the `Status` codes in the output CSV.

---

## Session 2 — 2025-07-24
**Tester:** User 2  
**Task:** Inspect the sample results CSV and compare with the source website  
**Observations:**
- User able to open `sample_results_success.csv` and `sample_results_failed.csv`.
- Verified 5 random URLs manually; claim dates matched the dataset source.
- Users appreciated asynchronous fetching; processing time was acceptable.

**Issues encountered:**
- Minor confusion about input file path; clarified in `README.md`.
- Suggested adding a brief note on updating BeautifulSoup parsing if sites change HTML layout.

**Comments:**
- Clear workflow.
- Users recommend adding an optional log output for skipped or failed URLs.

---

## Session 3 — 2025-08-03
**Tester:** User 3  
**Task:** Test run on Colab  
**Observations:**
- Mounted Google Drive successfully for CSV input.
- Script executed without crashes.
- Output CSV generated in `/content/drive/MyDrive/`.
- The user could easily check the `Claim Date` and `Status` columns.

**Issues encountered:**
- Initial internet instability caused one URL fetch to fail; retry logic handled it automatically.
- Script instructions in `README.md` are sufficient.

**Comments:**
- Users suggested providing a few screenshots in the user guide for clarity.
