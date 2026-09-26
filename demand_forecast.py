"""
demand_forecast.py

A demand-signal layer over the D2C stockout data.

Honest framing: with only ~22 days of history, there isn't enough data for
real statistical forecasting (ARIMA/Prophet need months to find seasonal
patterns reliably). What this script builds instead is three genuinely
useful signals that DO hold up with limited data:

  1. Stockout rate trend per brand -- is it getting better or worse,
     comparing the first half of the observed history to the second half.
  2. Restock cadence -- when a product goes out of stock, how many days
     does it typically take to come back? Faster cadence is itself a
     demand signal (brands replenish what's selling).
  3. Currently at-risk products -- what's out of stock right now, ranked
     by how many consecutive days it's been unavailable.

As more days of data accumulate, (1) and (2) get more reliable, and a
real forecasting model becomes reasonable to add on top of this.

Run: python demand_forecast.py
"""
import os
import sqlite3
import pandas as pd

DB_PATH = "data/db/price_intel.db"
OUT_DIR = "reports"


def load_data(conn):
    query = """
    SELECT p.variant_id, p.brand, p.product_title, p.category,
           f.snapshot_date, f.available
    FROM fact_price_daily f
    JOIN dim_product p ON f.variant_id = p.variant_id
    WHERE p.is_test = 0 AND p.is_freebie = 0 AND p.is_combo = 0
    ORDER BY p.variant_id, f.snapshot_date
    """
    df = pd.read_sql(query, conn, parse_dates=["snapshot_date"])
    return df


def stockout_trend_by_brand(df):
    """Compare stockout rate in the first half of history vs the second half."""
    dates = sorted(df["snapshot_date"].unique())
    midpoint = dates[len(dates) // 2]

    first_half = df[df["snapshot_date"] < midpoint]
    second_half = df[df["snapshot_date"] >= midpoint]

    def rate(d):
        return d.groupby("brand")["available"].apply(lambda x: (x == 0).mean() * 100)

    r1 = rate(first_half).rename("first_half_pct")
    r2 = rate(second_half).rename("second_half_pct")

    result = pd.concat([r1, r2], axis=1).round(2)
    result["trend_pts"] = (result["second_half_pct"] - result["first_half_pct"]).round(2)
    result["direction"] = result["trend_pts"].apply(
        lambda x: "worsening" if x > 1 else ("improving" if x < -1 else "stable")
    )
    return result.sort_values("trend_pts", ascending=False)


def restock_cadence(df):
    """For each variant, find OOS episodes and how many days they lasted before restock."""
    results = []
    for variant_id, g in df.groupby("variant_id"):
        g = g.sort_values("snapshot_date").reset_index(drop=True)
        in_oos = False
        start = None
        for _, row in g.iterrows():
            if row["available"] == 0 and not in_oos:
                in_oos = True
                start = row["snapshot_date"]
            elif row["available"] == 1 and in_oos:
                in_oos = False
                days = (row["snapshot_date"] - start).days
                results.append({"variant_id": variant_id, "brand": row["brand"], "restock_days": days})

    if not results:
        return pd.DataFrame(columns=["brand", "avg_restock_days", "n_completed_episodes"])

    rdf = pd.DataFrame(results)
    summary = rdf.groupby("brand")["restock_days"].agg(["mean", "count"]).round(1)
    summary.columns = ["avg_restock_days", "n_completed_restock_episodes"]
    return summary.sort_values("avg_restock_days")


def currently_at_risk(df):
    """Products unavailable as of the latest snapshot, with consecutive days OOS so far."""
    latest_date = df["snapshot_date"].max()
    currently_oos = df[(df["snapshot_date"] == latest_date) & (df["available"] == 0)]["variant_id"].unique()

    results = []
    for variant_id in currently_oos:
        g = df[df["variant_id"] == variant_id].sort_values("snapshot_date", ascending=False).reset_index(drop=True)
        days = 0
        for _, row in g.iterrows():
            if row["available"] == 0:
                days += 1
            else:
                break
        results.append({
            "variant_id": variant_id,
            "brand": g.iloc[0]["brand"],
            "product_title": g.iloc[0]["product_title"],
            "days_oos_so_far": days,
        })

    return pd.DataFrame(results).sort_values("days_oos_so_far", ascending=False)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    df = load_data(conn)
    conn.close()

    print(f"Loaded {len(df)} rows, {df['variant_id'].nunique()} variants, "
          f"{df['snapshot_date'].nunique()} days "
          f"({df['snapshot_date'].min().date()} to {df['snapshot_date'].max().date()})")
    print("NOTE: this is a trend/signal layer, not a statistical forecast -- "
          "the history so far is too short for real time-series modeling.\n")

    print("=== Stockout rate trend by brand (first half vs second half of history) ===")
    trend = stockout_trend_by_brand(df)
    print(trend.to_string())
    trend.to_csv(f"{OUT_DIR}/stockout_trend_by_brand.csv")

    print("\n=== Restock cadence by brand (avg days to come back after going OOS) ===")
    cadence = restock_cadence(df)
    print(cadence.to_string())
    cadence.to_csv(f"{OUT_DIR}/restock_cadence_by_brand.csv")

    print("\n=== Currently at-risk products (out of stock right now, longest first) ===")
    at_risk = currently_at_risk(df)
    print(at_risk.head(15).to_string(index=False))
    at_risk.to_csv(f"{OUT_DIR}/currently_at_risk.csv", index=False)

    print(f"\nSaved 3 CSVs to {OUT_DIR}/")


if __name__ == "__main__":
    main()