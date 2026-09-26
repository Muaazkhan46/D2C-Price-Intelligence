"""
stockout_revenue.py

Estimates revenue lost to stockouts. READ-ONLY analysis script.

Follows this project's data safety rules:
  - Opens price_intel.db in read-only mode -- structurally cannot write to it
  - Never touches collect.py, parse.py, load.py, or anything in data/
  - Only writes to reports/ (new analysis output, not pipeline data)

METHODOLOGY (stated explicitly -- there's no real sales/order data here,
only price + availability, so this is an estimate, not a measurement):

    For each day a variant is out of stock:
        lost_revenue = price_on_that_day * ASSUMED_DAILY_UNITS_SOLD

ASSUMED_DAILY_UNITS_SOLD is a single, explicit, adjustable assumption
(default: 1 unit/day/variant) -- a conservative floor. This is NOT a
claim about true demand. It answers: "if this product sells at least N
units/day when available, what's the estimated cost of it being
unavailable?" Change the constant below to test sensitivity -- the
brand-to-brand RANKING is more trustworthy than the absolute rupee figure.

SPLIT: results are reported separately for two very different situations:
  - CHURN: variants that have been in stock at least once and cycle in/out
    -- this is a genuine stock-management problem, and the operationally
    meaningful number.
  - PERSISTENT: variants that have NEVER been seen in stock across the
    whole observed history -- likely dead/discontinued stock, or added to
    tracking already OOS. Reporting this mixed into the main total
    overstates "lost sales from poor restocking," since these products may
    never come back regardless of stock management.

Run: python stockout_revenue.py
"""
import os
import sqlite3
import pandas as pd

DB_PATH = "data/db/price_intel.db"
OUT_DIR = "reports"
ASSUMED_DAILY_UNITS_SOLD = 1  # explicit, adjustable assumption -- see docstring


def load_data():
    # Read-only URI connection -- this script can never write to price_intel.db,
    # even by accident.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    query = """
    SELECT p.variant_id, p.brand, p.product_title, p.category,
           f.snapshot_date, f.available, f.price
    FROM fact_price_daily f
    JOIN dim_product p ON f.variant_id = p.variant_id
    WHERE p.is_test = 0 AND p.is_freebie = 0 AND p.is_combo = 0
    ORDER BY p.variant_id, f.snapshot_date
    """
    df = pd.read_sql(query, conn, parse_dates=["snapshot_date"])
    conn.close()
    return df


def classify_variants(df):
    """A variant is 'persistently unavailable' if it has NEVER been seen in
    stock across the whole observed history (likely dead/discontinued or
    added to tracking while already OOS) -- distinct from 'churn', where a
    variant cycles in and out of stock, which is what restock cadence
    actually measures."""
    ever_available = df.groupby("variant_id")["available"].max()  # 1 if ever in stock, 0 if never
    persistent_ids = ever_available[ever_available == 0].index
    churn_ids = ever_available[ever_available == 1].index
    return set(persistent_ids), set(churn_ids)


def revenue_by_brand(df, variant_ids=None):
    oos = df[df["available"] == 0].copy()
    if variant_ids is not None:
        oos = oos[oos["variant_id"].isin(variant_ids)]
    oos["lost_revenue"] = oos["price"] * ASSUMED_DAILY_UNITS_SOLD

    n_days = df["snapshot_date"].nunique()
    by_brand = oos.groupby("brand").agg(
        oos_days=("snapshot_date", "count"),
        estimated_lost_revenue=("lost_revenue", "sum"),
    ).round(0)
    by_brand["avg_daily_lost_revenue"] = (by_brand["estimated_lost_revenue"] / n_days).round(0)
    return by_brand.sort_values("estimated_lost_revenue", ascending=False)


def top_products_by_lost_revenue(df, variant_ids=None, n=10):
    oos = df[df["available"] == 0].copy()
    if variant_ids is not None:
        oos = oos[oos["variant_id"].isin(variant_ids)]
    oos["lost_revenue"] = oos["price"] * ASSUMED_DAILY_UNITS_SOLD

    by_product = oos.groupby(["variant_id", "brand", "product_title"]).agg(
        oos_days=("snapshot_date", "count"),
        estimated_lost_revenue=("lost_revenue", "sum"),
    ).round(0).reset_index()
    return by_product.sort_values("estimated_lost_revenue", ascending=False).head(n)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = load_data()

    n_days = df["snapshot_date"].nunique()
    print(f"Loaded {len(df)} rows, {df['variant_id'].nunique()} variants, {n_days} days")
    print(f"ASSUMPTION: {ASSUMED_DAILY_UNITS_SOLD} unit(s) sold/day/variant when in stock "
          f"-- edit ASSUMED_DAILY_UNITS_SOLD at the top of this script to test sensitivity.\n")

    persistent_ids, churn_ids = classify_variants(df)
    print(f"{len(persistent_ids)} variants have NEVER been seen in stock (persistently unavailable / likely dead stock)")
    print(f"{len(churn_ids)} variants have been in stock at least once (genuine churn -- these are what restock cadence measures)\n")

    print("=== CHURN-DRIVEN revenue loss by brand (the operationally meaningful number) ===")
    churn_brand = revenue_by_brand(df, variant_ids=churn_ids)
    print(churn_brand.to_string())
    churn_brand.to_csv(f"{OUT_DIR}/stockout_revenue_churn_by_brand.csv")
    churn_total = churn_brand["estimated_lost_revenue"].sum()

    print("\n=== PERSISTENTLY UNAVAILABLE revenue loss by brand (likely dead/discontinued stock, not a stock-management problem) ===")
    persistent_brand = revenue_by_brand(df, variant_ids=persistent_ids)
    print(persistent_brand.to_string())
    persistent_brand.to_csv(f"{OUT_DIR}/stockout_revenue_persistent_by_brand.csv")
    persistent_total = persistent_brand["estimated_lost_revenue"].sum()

    print("\n=== Top 10 CHURN products by estimated lost revenue ===")
    top_churn = top_products_by_lost_revenue(df, variant_ids=churn_ids)
    print(top_churn.to_string(index=False))
    top_churn.to_csv(f"{OUT_DIR}/stockout_revenue_top_churn_products.csv", index=False)

    print("\n=== Top 10 PERSISTENTLY UNAVAILABLE products by estimated lost revenue ===")
    top_persistent = top_products_by_lost_revenue(df, variant_ids=persistent_ids)
    print(top_persistent.to_string(index=False))
    top_persistent.to_csv(f"{OUT_DIR}/stockout_revenue_top_persistent_products.csv", index=False)

    print(f"\nChurn-driven lost revenue over {n_days} days: Rs {churn_total:,.0f} <- report this as the operational number")
    print(f"Persistently-unavailable lost revenue over {n_days} days: Rs {persistent_total:,.0f} <- likely dead stock, separate issue")
    print(f"Combined total: Rs {churn_total + persistent_total:,.0f}")
    print(f"\nSaved 6 CSVs to {OUT_DIR}/ -- price_intel.db and everything in data/ untouched.")


if __name__ == "__main__":
    main()