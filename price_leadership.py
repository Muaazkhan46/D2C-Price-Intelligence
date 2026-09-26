"""
price_leadership.py

Estimates which brand tends to move price first, for every pair of brands.
READ-ONLY analysis script -- same safety pattern as demand_forecast.py and
stockout_revenue.py: opens price_intel.db in read-only mode, never touches
data/, collect.py, parse.py, or load.py, and only writes to reports/.

METHODOLOGY (stated explicitly):
  There's no explicit competitor-product mapping in this data (no "DermaCo's
  sunscreen corresponds to Pilgrim's sunscreen" link), so this works at the
  SUBCATEGORY level instead of product-to-product: for each subcategory, on
  each day, did a given brand change ANY of its prices in that subcategory?
  That gives a daily 0/1 "did this brand move today" series per brand per
  subcategory.

  For every pair of brands sharing a subcategory, every time brand A moves,
  we look for the nearest day brand B also moved within a +/- LAG_WINDOW_DAYS
  window. The average signed gap tells you who tends to move first:
  positive means B tends to follow A (A leads); negative means A tends to
  follow B (B leads).

  subcategory is only populated for Skincare products in this dataset (see
  the knowledge base), so this analysis only covers Skincare subcategories --
  it says nothing about Makeup, Haircare, etc.

HONESTY, NOT JUST OUTPUT: a brand pair is only worth reading if there were
enough matched events behind it. This script reports n_matched_events for
every pair and flags anything below MIN_EVENTS_FOR_CONFIDENCE as
"insufficient data" rather than a real finding -- brands that barely change
price (DermaCo, Foxtale as of this writing) will correctly show up this way,
and that absence of signal is itself a real, reportable result: it means
those brands don't compete on listed price the way Pilgrim and Minimalist do
(they may lean on discount_pct/compare_at_price instead, worth checking
separately).

Run: python price_leadership.py
"""
import os
import sqlite3
import pandas as pd
from itertools import combinations

DB_PATH = "data/db/price_intel.db"
OUT_DIR = "reports"
LAG_WINDOW_DAYS = 5          # how far to look for a matching move from the other brand
MIN_EVENTS_FOR_CONFIDENCE = 15  # below this, a pair's result is flagged, not trusted


def load_data():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    query = """
    SELECT p.brand, p.subcategory, f.variant_id, f.snapshot_date, f.price
    FROM fact_price_daily f
    JOIN dim_product p ON f.variant_id = p.variant_id
    WHERE p.is_test = 0 AND p.is_freebie = 0 AND p.is_combo = 0
      AND p.is_campaign = 0 AND p.is_flash_sale = 0
      AND p.subcategory IS NOT NULL
    ORDER BY p.variant_id, f.snapshot_date
    """
    df = pd.read_sql(query, conn, parse_dates=["snapshot_date"])
    conn.close()
    return df


def daily_move_series(df):
    """For each (subcategory, brand, date): did ANY variant change price that day?"""
    df = df.sort_values(["variant_id", "snapshot_date"])
    df["prev_price"] = df.groupby("variant_id")["price"].shift(1)
    df["moved"] = (df["prev_price"].notna()) & (df["price"] != df["prev_price"])

    moves = (
        df[df["moved"]]
        .groupby(["subcategory", "brand", "snapshot_date"])
        .size()
        .reset_index(name="n_variants_moved")
    )
    return moves


def pairwise_leadership(moves):
    results = []
    for subcat, sub_df in moves.groupby("subcategory"):
        brands = sorted(sub_df["brand"].unique())
        for brand_a, brand_b in combinations(brands, 2):
            a_days = sub_df.loc[sub_df["brand"] == brand_a, "snapshot_date"].sort_values().tolist()
            b_days = sub_df.loc[sub_df["brand"] == brand_b, "snapshot_date"].sort_values().tolist()
            if not a_days or not b_days:
                continue

            gaps = []
            for day_a in a_days:
                # nearest B move within the window, signed: positive = B came after A
                candidates = [(day_b - day_a).days for day_b in b_days
                              if abs((day_b - day_a).days) <= LAG_WINDOW_DAYS]
                if candidates:
                    nearest = min(candidates, key=abs)
                    gaps.append(nearest)

            if not gaps:
                continue

            avg_gap = sum(gaps) / len(gaps)
            n_events = len(gaps)
            if avg_gap > 0.3:
                leader = brand_a
            elif avg_gap < -0.3:
                leader = brand_b
            else:
                leader = "no clear leader"

            results.append({
                "subcategory": subcat,
                "brand_a": brand_a,
                "brand_b": brand_b,
                "n_matched_events": n_events,
                "avg_lag_days": round(avg_gap, 2),
                "apparent_leader": leader,
                "confidence": "OK" if n_events >= MIN_EVENTS_FOR_CONFIDENCE else "INSUFFICIENT DATA",
            })

    return pd.DataFrame(results).sort_values(["subcategory", "n_matched_events"], ascending=[True, False])


def brand_move_frequency(df):
    """Supporting context: how often does each brand move at all, per subcategory?"""
    df = df.sort_values(["variant_id", "snapshot_date"])
    df["prev_price"] = df.groupby("variant_id")["price"].shift(1)
    df["moved"] = (df["prev_price"].notna()) & (df["price"] != df["prev_price"])
    summary = (
        df.groupby(["subcategory", "brand"])
        .agg(n_variants=("variant_id", "nunique"), n_price_changes=("moved", "sum"))
        .reset_index()
    )
    summary["changes_per_variant"] = (summary["n_price_changes"] / summary["n_variants"]).round(2)
    return summary.sort_values(["subcategory", "n_price_changes"], ascending=[True, False])


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = load_data()
    n_days = df["snapshot_date"].nunique()
    print(f"Loaded {len(df)} rows, {df['variant_id'].nunique()} variants, {n_days} days "
          f"(Skincare subcategories only -- subcategory is null elsewhere)")
    print(f"Lag window: +/-{LAG_WINDOW_DAYS} days. A pair needs >= {MIN_EVENTS_FOR_CONFIDENCE} "
          f"matched events to be marked OK rather than INSUFFICIENT DATA.\n")

    freq = brand_move_frequency(df)
    print("=== How often each brand changes price, by subcategory ===")
    print(freq.to_string(index=False))
    freq.to_csv(f"{OUT_DIR}/price_change_frequency_by_subcategory.csv", index=False)

    moves = daily_move_series(df)
    leadership = pairwise_leadership(moves)

    print("\n=== Pairwise price-move leadership by subcategory ===")
    if leadership.empty:
        print("No brand pairs had any matched events within the lag window.")
    else:
        print(leadership.to_string(index=False))
    leadership.to_csv(f"{OUT_DIR}/price_leadership_pairs.csv", index=False)

    n_confident = (leadership["confidence"] == "OK").sum() if not leadership.empty else 0
    n_total = len(leadership)
    print(f"\n{n_confident} of {n_total} brand-pair/subcategory combinations have enough "
          f"matched events to trust; the rest are reported but flagged INSUFFICIENT DATA "
          f"rather than treated as a finding.")
    print(f"\nSaved 2 CSVs to {OUT_DIR}/ -- price_intel.db and everything in data/ untouched.")


if __name__ == "__main__":
    main()