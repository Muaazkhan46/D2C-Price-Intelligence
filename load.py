"""
load.py - Load the parsed table into SQLite as a star schema.

Two tables:

  dim_product      one row per variant. Descriptive, current values.
                   first_seen / last_seen give launch and delist dates.

  fact_price_daily one row per variant per day. The measurements.
                   Primary key (variant_id, snapshot_date) - the grain.

Uses INSERT OR REPLACE, so re-running is safe: an existing row is
overwritten rather than duplicated. The database ends up in the same
state whether you run this once or ten times.

Run after parse.py.
"""

import sqlite3
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------

PARSED_FILE = Path("data/parsed/variants_daily.csv")
DB_DIR = Path("data/db")
DB_FILE = DB_DIR / "price_intel.db"


SCHEMA = """
-- Descriptive attributes. One row per variant, current values only.
CREATE TABLE IF NOT EXISTS dim_product (
    variant_id      INTEGER PRIMARY KEY,
    product_id      INTEGER,
    brand           TEXT    NOT NULL,
    product_title   TEXT,
    variant_title   TEXT,
    product_type    TEXT,
    category        TEXT,      -- Skincare / Makeup / Haircare / ...
    subcategory     TEXT,      -- Serum / Sunscreen / ... (Skincare only)
    sku             TEXT,
    grams           INTEGER,
    is_freebie      INTEGER NOT NULL DEFAULT 0,
    is_combo        INTEGER NOT NULL DEFAULT 0,
    is_test         INTEGER NOT NULL DEFAULT 0,
    is_campaign     INTEGER NOT NULL DEFAULT 0,
    is_flash_sale   INTEGER NOT NULL DEFAULT 0,
    published_at    TEXT,
    first_seen      TEXT,      -- first snapshot we saw it  -> launch
    last_seen       TEXT       -- last snapshot we saw it   -> delist
);

-- Measurements. One row per variant per day. This is the grain.
CREATE TABLE IF NOT EXISTS fact_price_daily (
    variant_id       INTEGER NOT NULL,
    snapshot_date    TEXT    NOT NULL,
    price            REAL,
    compare_at_price REAL,
    discount_pct     REAL,
    available        INTEGER NOT NULL,
    PRIMARY KEY (variant_id, snapshot_date),
    FOREIGN KEY (variant_id) REFERENCES dim_product (variant_id)
);

-- Indexes for the queries we will actually run
CREATE INDEX IF NOT EXISTS ix_fact_date  ON fact_price_daily (snapshot_date);
CREATE INDEX IF NOT EXISTS ix_dim_brand  ON dim_product (brand);
CREATE INDEX IF NOT EXISTS ix_dim_cat    ON dim_product (category, subcategory);
"""


# ---------------------------------------------------------------

def build_dimension(df):
    """
    One row per variant, holding its CURRENT attributes.

    Overwrite, not history: if a title changes we keep the newest. The
    analysis is on price and stock, so a date-ranged join for historical
    titles would cost every query and buy nothing.

    first_seen / last_seen preserve the dates that DO matter.
    """
    df = df.sort_values("snapshot_date")

    # Latest row per variant = current attributes
    latest = df.groupby("variant_id", as_index=False).last()

    seen = df.groupby("variant_id", as_index=False).agg(
        first_seen=("snapshot_date", "min"),
        last_seen=("snapshot_date", "max"),
    )

    dim = latest.merge(seen, on="variant_id", how="left")

    return dim[[
        "variant_id", "product_id", "brand", "product_title", "variant_title",
        "product_type", "category", "subcategory", "sku", "grams",
        "is_freebie", "is_combo", "is_test", "is_campaign", "is_flash_sale",
        "published_at", "first_seen", "last_seen",
    ]]


def build_fact(df):
    """One row per variant per day - just the measurements."""
    return df[[
        "variant_id", "snapshot_date", "price", "compare_at_price",
        "discount_pct", "available",
    ]]


def upsert(conn, table, frame, columns):
    """
    INSERT OR REPLACE row by row.

    Not df.to_sql(): that would either append duplicates or drop the
    table. REPLACE respects the primary key, so re-running updates in
    place. That is what makes this script safe to run repeatedly.
    """
    placeholders = ", ".join("?" for _ in columns)
    sql = (f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
           f"VALUES ({placeholders})")

    rows = frame.where(pd.notna(frame), None).values.tolist()
    conn.executemany(sql, rows)
    return len(rows)


def main():
    if not PARSED_FILE.exists():
        raise SystemExit(f"{PARSED_FILE} not found. Run parse.py first.")

    df = pd.read_csv(PARSED_FILE)
    print(f"Read {len(df):,} rows from {PARSED_FILE}\n")

    # SQLite has no boolean type - store flags as 0/1
    for col in ["available", "is_freebie", "is_combo", "is_test",
                "is_campaign", "is_flash_sale"]:
        df[col] = df[col].astype(int)

    dim = build_dimension(df)
    fact = build_fact(df)

    DB_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_FILE) as conn:
        conn.executescript(SCHEMA)

        n_dim = upsert(conn, "dim_product", dim, list(dim.columns))
        n_fact = upsert(conn, "fact_price_daily", fact, list(fact.columns))
        conn.commit()

        print(f"dim_product:      {n_dim:,} rows upserted")
        print(f"fact_price_daily: {n_fact:,} rows upserted")

        # ---- Verify against the database, not against the dataframe ----
        # Checking what actually landed is the point of a load step.
        q = lambda sql: conn.execute(sql).fetchone()

        print(f"\n--- In {DB_FILE} ---")
        print(f"  dim_product rows:      {q('SELECT COUNT(*) FROM dim_product')[0]:,}")
        print(f"  fact rows:             {q('SELECT COUNT(*) FROM fact_price_daily')[0]:,}")
        print(f"  distinct days:         "
              f"{q('SELECT COUNT(DISTINCT snapshot_date) FROM fact_price_daily')[0]}")

        # Orphans: fact rows with no matching dimension row. Should be 0.
        orphans = q("""
            SELECT COUNT(*) FROM fact_price_daily f
            LEFT JOIN dim_product d ON d.variant_id = f.variant_id
            WHERE d.variant_id IS NULL
        """)[0]
        print(f"  orphan fact rows:      {orphans}")
        if orphans:
            print("    WARNING: fact rows without a product. Investigate.")

        print("\n--- Latest day: clean SKINCARE by brand ---")
        for brand, n, oos in conn.execute("""
            SELECT d.brand,
                   COUNT(*)                        AS products,
                   SUM(CASE WHEN f.available = 0 THEN 1 ELSE 0 END) AS out_of_stock
            FROM   fact_price_daily f
            JOIN   dim_product d ON d.variant_id = f.variant_id
            WHERE  d.is_freebie = 0 AND d.is_combo = 0
              AND  d.is_test = 0 AND d.is_campaign = 0
              AND  d.is_flash_sale = 0
              AND  d.category = 'Skincare'
              AND  f.snapshot_date = (SELECT MAX(snapshot_date)
                                      FROM fact_price_daily)
            GROUP  BY d.brand
            ORDER  BY products DESC
        """):
            print(f"  {brand:<12} {n:>5} products   {oos:>4} out of stock")

    print(f"\nDatabase ready: {DB_FILE}")


if __name__ == "__main__":
    main()