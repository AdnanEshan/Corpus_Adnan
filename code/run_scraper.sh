#!/bin/bash
# run_scraper.sh — run the claim date extractor

# Optional: activate a Python virtual environment
# source venv/bin/activate

# Run the main scraper script
python code/claim_date_extractor.py --input data/input_urls.csv --output data/sample_results.csv

# Optional: deactivate the virtual environment
# deactivate
