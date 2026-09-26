"""
explore.py - Quick sanity checks on the parsed table.

Not part of the pipeline. This is the file you run when you want to
LOOK at the data - after a parser change, or when a number in the
summary looks surprising.
"""

import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 55)

df = pd.read_csv("data/parsed/variants_daily.csv")

# Work with the latest day only
latest = df["snapshot_date"].max()
day = df[df["snapshot_date"] == latest]

print(f"=== Snapshot: {latest} | {len(day):,} rows ===\n")


# --- 1. Rows by brand, split by flag -----------------------------
print("--- Rows by brand ---")
by_brand = day.groupby("brand").agg(
    total=("variant_id", "count"),
    freebies=("is_freebie", "sum"),
    combos=("is_combo", "sum"),
)
by_brand["clean"] = by_brand["total"] - by_brand["freebies"] - by_brand["combos"]
print(by_brand, "\n")


# --- 2. Stockouts by brand (clean products only) -----------------
clean = day[~day["is_freebie"] & ~day["is_combo"]]

print("--- Out of stock by brand (clean products) ---")
stock = clean.groupby("brand").agg(
    products=("variant_id", "count"),
    out_of_stock=("available", lambda s: (~s).sum()),
)
stock["oos_pct"] = (stock["out_of_stock"] / stock["products"] * 100).round(1)
print(stock.sort_values("oos_pct", ascending=False), "\n")


# --- 3. What is actually out of stock right now ------------------
print("--- Out-of-stock products (top 15 by price) ---")
oos = clean[~clean["available"]].sort_values("price", ascending=False)
print(oos[["brand", "product_title", "variant_title", "price"]].head(15), "\n")


# --- 4. Is the combo flag firing correctly? ----------------------
# The '+' rule may be catching ingredient lists, not real bundles.
print("--- Sample of rows flagged as COMBO (check these) ---")
combos = day[day["is_combo"]].drop_duplicates("product_title")
print(combos[["brand", "product_title", "product_type", "price"]].head(20), "\n")


# --- 5. Sample of freebies ---------------------------------------
print("--- Sample of rows flagged as FREEBIE ---")
free = day[day["is_freebie"]].drop_duplicates("product_title")
print(free[["brand", "product_title", "product_type", "price"]].head(10), "\n")


# --- 6. Discounting right now ------------------------------------
print("--- Discounts by brand (clean products) ---")
disc = clean.groupby("brand").agg(
    on_discount=("discount_pct", lambda s: (s > 0).sum()),
    products=("discount_pct", "count"),
    avg_discount_when_on=("discount_pct", lambda s: s[s > 0].mean()),
)
disc["pct_on_discount"] = (disc["on_discount"] / disc["products"] * 100).round(1)
disc["avg_discount_when_on"] = disc["avg_discount_when_on"].round(1)
print(disc, "\n")


# --- 7. The most expensive thing, sanity check -------------------
print("--- 5 most expensive clean products ---")
print(clean.nlargest(5, "price")[["brand", "product_title", "variant_title", "price"]])