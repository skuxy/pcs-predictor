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
RACE_CLASSES = ["2.UWT", "2.HC", "2.1", "1.UWT", "1.HC", "1.Pro", "2.Pro"]

# PCS circuit numbers + allowed classes per circuit for men's races.
# circuit 1  = UCI WorldTour (1.UWT, 2.UWT)
# circuit 16 = UCI ProSeries (1.Pro, 2.Pro only — 2.1 on this circuit includes women's races)
MEN_CIRCUITS = {"1": RACE_CLASSES, "16": ["1.Pro", "2.Pro"]}

# Women's WorldTour class codes (circuit=24 on PCS)
WOMEN_RACE_CLASSES = ["2.WWT", "1.WWT"]
WOMEN_CIRCUIT = "24"

# Race slugs whose stages are predominantly cobbled
COBBLED_RACE_SLUGS = [
    "paris-roubaix",
    "ronde-van-vlaanderen",
    "e3-saxo",
    "e3-harelbeke",
    "dwars-door-vlaanderen",
    "omloop-het-nieuwsblad",
    "classic-brugge-de-panne",
    "bredene-koksijde",
    "nokere-koerse",
    "gp-de-denain",
]

# Race slugs that are predominantly gravel
GRAVEL_RACE_SLUGS = [
    "strade-bianche",
]
