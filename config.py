PCS_BASE = "https://www.procyclingstats.com"

# Scraping behaviour
REQUEST_DELAY = 1.5        # seconds between requests
REQUEST_TIMEOUT = 30       # seconds
MAX_RETRIES = 3
CACHE_DIR = "cache/html"   # raw HTML cache to avoid re-fetching
DB_PATH = "data/cycling.db"

# Data range
SCRAPE_YEARS = [2023, 2024]  # years of results to collect

# Race tiers to include (PCS classification codes)
# 2.UWT = UCI WorldTour, 2.HC = Hors Categorie, 2.1 = first division
RACE_CLASSES = ["2.UWT", "2.HC", "2.1", "1.UWT", "1.HC"]
