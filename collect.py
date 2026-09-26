"""
collect.py - Daily raw snapshot collector (Shopify JSON version).

Fetches each brand's public /products.json feed and saves the response
exactly as received. It does NOT extract or clean anything - that
happens later in parse.py, reading these saved files.

Why save raw: if extraction logic is wrong, we fix parse.py and re-run
it over every day ever collected. A day of data is never lost to a bug.

Run once per day.
"""

import csv
import json
import random
import time
from datetime import date
from pathlib import Path

import requests

# ---------------------------------------------------------------
# CONFIG - the only part you edit
# ---------------------------------------------------------------

RAW_DIR = Path("data/raw")
BRAND_FILE = Path("brands.csv")      # columns: brand,url
LOG_FILE = RAW_DIR / "collection_log.csv"

MIN_DELAY = 3        # seconds between requests
MAX_DELAY = 6
TIMEOUT = 30
RETRIES = 3
MAX_PAGES = 10       # safety cap: 10 pages x 250 = 2500 products per brand

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


# ---------------------------------------------------------------

def load_brands():
    """Read the brand list. Returns a list of dicts."""
    if not BRAND_FILE.exists():
        raise SystemExit(
            f"{BRAND_FILE} not found. Create it with columns: brand,url"
        )
    with open(BRAND_FILE, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("brand", "").strip()]
    if not rows:
        raise SystemExit(f"{BRAND_FILE} has no data rows.")
    return rows


def fetch_json(url):
    """Fetch one URL with retries. Returns (parsed_json, None) or (None, error)."""
    last_error = "unknown"
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json(), None
            last_error = f"http_{r.status_code}"
        except requests.RequestException as e:
            last_error = type(e).__name__
        except json.JSONDecodeError:
            # Got a response, but it wasn't JSON - usually a block page
            return None, "not_json"

        if attempt < RETRIES:
            time.sleep(attempt * 5)      # back off longer each retry
    return None, last_error


def fetch_all_pages(base_url):
    """
    Shopify caps each response at 250 products, so walk pages until one
    comes back empty. Returns (products, error, partial).

    'partial' is True when an early page succeeded but a later one
    failed. We keep what we got - partial data beats none - but the
    caller MUST record that it was partial. A silent partial success is
    worse than a clean failure, because nothing downstream can tell.
    """
    products = []
    for page in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}page={page}"

        data, err = fetch_json(url)
        if err:
            if page == 1:
                return None, err, False        # clean failure, nothing saved
            return products, None, True        # partial - flag it

        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)

        if len(batch) < 250:
            break

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    return products, None, False


def previous_counts(day_dir):
    """
    Variant counts per brand from the most recent PREVIOUS day.
    Used to sanity-check today's numbers.
    """
    prior_days = sorted(d for d in RAW_DIR.iterdir()
                        if d.is_dir() and d.name < day_dir.name)
    if not prior_days:
        return {}

    counts = {}
    for json_file in prior_days[-1].glob("*.json"):
        try:
            products = json.loads(json_file.read_text(encoding="utf-8"))
            counts[json_file.stem] = count_variants(products)
        except (json.JSONDecodeError, OSError):
            continue
    return counts


def log_row(run_date, brand, status, note=""):
    """Append one line to the collection log so failures stay visible."""
    new_file = not LOG_FILE.exists()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["run_date", "brand", "status", "note"])
        w.writerow([run_date, brand, status, note])


def count_variants(products):
    """How many variants across all products - the real row count."""
    return sum(len(p.get("variants", [])) for p in products)


def main():
    run_date = date.today().isoformat()
    day_dir = RAW_DIR / run_date
    day_dir.mkdir(parents=True, exist_ok=True)

    brands = load_brands()
    print(f"{run_date}: collecting {len(brands)} brands\n")

    # A count that is fine yesterday and tiny today is usually a broken
    # collection, not a real delisting. Load yesterday's counts to compare.
    prev = previous_counts(day_dir)
    DROP_THRESHOLD = 0.10          # warn if a brand loses >10% of variants

    saved = skipped = failed = suspect = 0
    total_variants = 0

    for i, b in enumerate(brands, start=1):
        brand = b["brand"].strip()
        out_path = day_dir / f"{brand}.json"

        # Already collected today - skip. Makes the script safe to re-run
        # after a crash without refetching what we already have.
        if out_path.exists():
            print(f"[{i}/{len(brands)}] skip    {brand} (already have today)")
            skipped += 1
            continue

        products, err, partial = fetch_all_pages(b["url"].strip())

        if err or products is None:
            log_row(run_date, brand, "fail", err)
            failed += 1
            print(f"[{i}/{len(brands)}] FAILED  {brand}  ({err})")
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            continue

        n_var = count_variants(products)

        # ---- Plausibility check ------------------------------------
        # Logging WHETHER a fetch succeeded is not the same as logging
        # whether the RESULT is believable. This checks the second.
        yesterday = prev.get(brand)
        dropped = (yesterday is not None
                   and n_var < yesterday * (1 - DROP_THRESHOLD))

        if partial or dropped:
            status = "ok_suspect"
            reason = "pagination cut short" if partial else \
                     f"variants {yesterday} -> {n_var}"
            suspect += 1
            print(f"[{i}/{len(brands)}] SUSPECT {brand}  "
                  f"{len(products)} products, {n_var} variants  ({reason})")
            print(f"          -> delete {out_path} and re-run to retry")
        else:
            status = "ok"
            reason = ""
            saved += 1
            print(f"[{i}/{len(brands)}] ok      {brand}  "
                  f"{len(products)} products, {n_var} variants")

        out_path.write_text(
            json.dumps(products, ensure_ascii=False), encoding="utf-8"
        )
        total_variants += n_var
        note = f"{len(products)} products / {n_var} variants"
        log_row(run_date, brand, status, f"{note} {reason}".strip())

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    print(f"\nDone. saved={saved} suspect={suspect} "
          f"skipped={skipped} failed={failed}")
    print(f"Rows this day will be: {total_variants} variants")
    print(f"Files in: {day_dir}")

    if failed or suspect:
        print(f"\nWARNING: {failed} failed, {suspect} suspect. "
              f"Check {LOG_FILE}")
        print("Suspect files ARE saved but may be incomplete. "
              "Delete them and re-run to retry.")


if __name__ == "__main__":
    main()