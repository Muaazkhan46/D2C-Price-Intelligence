"""
parse.py - Flatten raw Shopify JSON snapshots into one tidy table.

Reads EVERY day in data/raw/ and rebuilds data/parsed/variants_daily.csv
from scratch each run. That is deliberate: if we improve extraction
later, re-running backfills every historical day. Raw is the source of
truth; this file is disposable and rebuildable.

Grain: one row per variant per snapshot_date.

Run after collect.py, or any time this script changes.
"""

import json
import re
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------

RAW_DIR = Path("data/raw")
OUT_DIR = Path("data/parsed")
OUT_FILE = OUT_DIR / "variants_daily.csv"

# --- Permanent patch for a known, unrecoverable raw-data gap ------
# Pilgrim's raw JSON for 2026-08-25 was deleted by accident, AFTER a
# backup of that day's already-parsed rows was taken. The raw source
# can never be regenerated, so this restores those rows automatically
# on every run. No separate manual step is ever needed - this file is
# the single place the fix lives.
LOST_RAW_PATCHES = [
    {
        "brand": "Pilgrim",
        "date": "2026-08-25",
        "backup_file": Path("data/parsed/variants_daily_backup_aug25.csv"),
    },
]

# Anything priced below this is not a real purchase price.
# Judgement call - state it, don't hide it.
MIN_REAL_PRICE = 50

# "Oil Free", "Fragrance-Free" etc are product CLAIMS, not giveaways.
# Strip them before looking for the word "free".
CLAIM_FREE_PAT = re.compile(
    r"\b(oil|fragrance|alcohol|sulphate|sulfate|paraben|cruelty|gluten"
    r"|silicone|talc|soap|dye|grease|acne)[\s-]*free\b",
    re.IGNORECASE,
)

FREEBIE_TYPES = {"freebie", "free gift", "gift", "sample"}
FREEBIE_TAG_WORDS = {"free gift", "freebie", "mystery box", "gwp"}
FREEBIE_TITLE_PAT = re.compile(
    r"\bfree\b|mystery\s*box|\bsample\b|\btester\b|\bgwp\b",
    re.IGNORECASE,
)

# --- Combo rules --------------------------------------------------
# NOTE: '+' was removed. Skincare names actives that way
# ("Ceramide + HA Moisturizer") and it is one product, not a bundle.
# Tags are NOT used: brands put navigation filters like
# "combo_filter_facewash" in tags, which are not product descriptions.
COMBO_TYPES = {"combo", "combos", "kit", "kits", "set", "sets",
               "duo", "duos", "bundle", "bundles", "regimen"}
COMBO_TITLE_PAT = re.compile(
    r"\bcombo\b|\bkit\b|\bduo\b|\btrio\b|\bbundle\b|\bpack of\b"
    r"|\bregimen\b|\bhamper\b|\bgift\s*set\b|\bessentials\b|\bsquad\b"
    r"|\broutine\b|\bshowstopper\b|\bpower\s*player\b|\bromantic\b",
    re.IGNORECASE,
)

# --- Test / staging products left live in a brand's feed ----------
TEST_TITLE_PAT = re.compile(
    r"\btest\b|\bdev\b|\bdemo\b|\bdummy\b|\bstaging\b", re.IGNORECASE
)

# --- Campaign clones ----------------------------------------------
# Pilgrim duplicates the SAME physical product as separate listings for
# ad channels ("Liquid Lipstick - Acquisition - Meta"). These are
# PERMANENT clones - they sit in the catalogue indefinitely.
CAMPAIGN_PAT = re.compile(
    r"acquisition|liquidation|\bmeta\b|\bgpay\b|\bpaytm\b|googleads"
    r"|google\s*ads|\bcoupon\b|buy\s*any",
    re.IGNORECASE,
)

# --- Flash sale listings ------------------------------------------
# A different thing from a campaign clone: a DATED EVENT. On 2026-08-24
# Pilgrim published ~95 variants in under two minutes, all titled
# "... - Rs 77 Flash Sale". Own flag, because first_seen/last_seen on
# these rows give the sale's start and end. Folded into is_campaign,
# that event would be invisible among the permanent clones.
FLASH_SALE_PAT = re.compile(
    r"flash\s*sale|\bdeal\s*of\s*the\s*day\b|\bmidnight\s*sale\b"
    r"|₹\s*\d+\s*(sale|deal)|\brs\.?\s*\d+\s*(sale|deal)",
    re.IGNORECASE,
)

# --- Category rules -----------------------------------------------
# Derived from TITLE first, product_type second.
#
# Why title: product_type is an internal field each brand abuses
# differently (Minimalist tags everything "Skin Care"; Pilgrim puts ad
# campaigns in it; 194 rows are blank). Titles are customer-facing, so
# brands keep them consistent. Prefer the field the source has an
# incentive to keep clean.
#
# ORDER MATTERS. "Hair Serum" and "Body Lotion" contain skincare words,
# so hair and body are tested BEFORE skincare.
CATEGORY_RULES = [
    ("Fragrance", r"\bperfume\b|eau\s*de\s*parfum|body\s*mist|\bfragrance\b"),
    ("Accessories", r"t-?shirt|\bpouch\b|\bcandle\b|sunglass|jewell?ery"
                    r"|pillow|\btote\b|\bbag\b|\bbox\b"),
    ("Haircare", r"\bhair\b|\bshampoo\b|\bconditioner\b|\bscalp\b|dandruff"
                 r"|rosemary\s*water|\bfrizz\b"),
    ("Bodycare", r"\bbody\b|\bunderarm\b|roll-?on|hand\s*cream|\bfoot\b"
                 r"|\bsoap\b|deodorant|\bbath\b"),
    ("Makeup",   r"lipstick|\blip\s*crayon\b|foundation|compact|concealer"
                 r"|\bblush\b|eyeshadow|eyeliner|mascara|\bbb\s*cream\b"
                 r"|\bprimer\b|highlighter|\bkajal\b|setting\s*spray"
                 r"|\bpalette\b|loose\s*powder|\btint\b"),
    ("Skincare", r"\bserum\b|sunscreen|\bspf\b|moistur|\bcleanser\b"
                 r"|face\s*wash|\btoner\b|face\s*mist|\bmask\b|\bscrub\b"
                 r"|under[\s-]*eye|eye\s*cream|\bpeel\b|exfoliat|\bcream\b"
                 r"|\bgel\b|\bacne\b|lip\s*balm|cleansing\s*balm|face\s*oil"
                 r"|\blotion\b|\bpatch\b|\bmicellar\b|cleansing\s*oil"
                 r"|\bointment\b|\bdrops\b|lip\s*treatment|\bemulsion\b"
                 r"|\bessence\b|spot\s*reduction|\bhydrat|\bbrighten"),
]
CATEGORY_RULES = [(name, re.compile(pat, re.IGNORECASE))
                  for name, pat in CATEGORY_RULES]

# Sub-category, only meaningful within Skincare. This is the level that
# makes cross-brand price comparison possible ("30ml vitamin C serums").
SUBCATEGORY_RULES = [
    ("Sunscreen",   r"sunscreen|\bspf\b"),
    ("Serum",       r"\bserum\b|\bampoule\b"),
    ("Cleanser",    r"\bcleanser\b|face\s*wash|cleansing|\bfw\b"),
    ("Moisturizer", r"moistur|\bcream\b(?!.*\bbb\b)|\blotion\b"),
    ("Toner",       r"\btoner\b|face\s*mist|\bessence\b"),
    ("Exfoliant",   r"\bpeel\b|exfoliat|\bscrub\b|\baha\b|\bbha\b"),
    ("EyeCare",     r"under[\s-]*eye|eye\s*cream|eye\s*gel"),
    ("Mask",        r"\bmask\b|\bpatch\b"),
    ("LipCare",     r"lip\s*balm|lip\s*treatment"),
    ("FaceOil",     r"face\s*oil|\bsqualane\b|cleansing\s*balm"
                    r"|cleansing\s*oil|\bointment\b|massage\s*oil"),
    ("SpotTreat",   r"spot\s*reduction|\bdrops\b|\bspot\s*corrector\b"),
]
SUBCATEGORY_RULES = [(name, re.compile(pat, re.IGNORECASE))
                     for name, pat in SUBCATEGORY_RULES]


# ---------------------------------------------------------------

def to_number(value):
    """
    Shopify sends price as a STRING ("899.00") and MRP is often null.
    Return a float, or None if it genuinely isn't a number.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tags_to_text(tags):
    """Tags arrive as a list. Flatten to a lowercase, comma-joined string."""
    if isinstance(tags, list):
        return ", ".join(str(t) for t in tags).lower()
    if isinstance(tags, str):
        return tags.lower()
    return ""


def is_freebie(title, product_type, tag_text, price):
    """
    True when this listing is a giveaway rather than something sold.

    Evidence hierarchy: what the brand DECLARES (product_type) outranks
    what we INFER from the title. A title keyword alone should not
    override a real category the brand assigned.
    """
    ptype = product_type.strip().lower()

    # 1. Price. Nothing real sells for Rs 0 or Rs 1.
    if price is not None and price < MIN_REAL_PRICE:
        return True

    # 2. The brand said so outright.
    if ptype in FREEBIE_TYPES:
        return True
    if any(word in tag_text for word in FREEBIE_TAG_WORDS):
        return True

    # 3. Title inference - but ONLY when the brand gave no real category.
    #    Strip claim phrases first ("Oil Free" is a claim, not a gift).
    if ptype == "":
        cleaned = CLAIM_FREE_PAT.sub(" ", title or "")
        if FREEBIE_TITLE_PAT.search(cleaned):
            return True

    return False


def is_combo(title, product_type, tag_text):
    """True when this listing bundles multiple items into one price."""
    if product_type.strip().lower() in COMBO_TYPES:
        return True
    if COMBO_TITLE_PAT.search(title or ""):
        return True
    return False


def is_test(title):
    """True for staging/test listings a brand left visible in its feed."""
    return bool(TEST_TITLE_PAT.search(title or ""))


def is_campaign(title, product_type):
    """
    True when this listing is an ad-channel clone of another product.
    Pilgrim's "Liquid Lipstick - Acquisition - Meta" is the same lipstick
    as the normal listing, with its own variant_id and price.
    """
    text = f"{title or ''} {product_type or ''}"
    return bool(CAMPAIGN_PAT.search(text))


def is_flash_sale(title, product_type):
    """True for time-boxed promotional listings, e.g. '- Rs 77 Flash Sale'."""
    return bool(FLASH_SALE_PAT.search(f"{title or ''} {product_type or ''}"))


def classify(title, product_type):
    """
    Return (category, subcategory).

    Title is checked first because it is customer-facing and therefore
    consistent across brands. product_type is the fallback for the ~194
    rows where the title alone is ambiguous or the field is blank.
    """
    text = f"{title or ''} {product_type or ''}"

    category = "Other"
    for name, pattern in CATEGORY_RULES:
        if pattern.search(text):
            category = name
            break

    subcategory = ""
    if category == "Skincare":
        for name, pattern in SUBCATEGORY_RULES:
            if pattern.search(text):
                subcategory = name
                break
        if not subcategory:
            subcategory = "OtherSkincare"

    return category, subcategory


def flatten_day(day_dir):
    """Turn one day's JSON files into a list of flat variant dicts."""
    snapshot_date = day_dir.name          # folder name IS the date
    rows = []

    for json_file in sorted(day_dir.glob("*.json")):
        brand = json_file.stem            # filename IS the brand

        try:
            products = json.loads(json_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  ! {snapshot_date}/{json_file.name} is not valid JSON - skipped")
            continue

        for p in products:
            # Product-level fields, read once per product
            title = p.get("title") or ""
            product_type = p.get("product_type") or ""
            tag_text = tags_to_text(p.get("tags"))
            category, subcategory = classify(title, product_type)
            campaign_flag = is_campaign(title, product_type)
            flash_flag = is_flash_sale(title, product_type)

            for v in p.get("variants", []):
                price = to_number(v.get("price"))
                mrp = to_number(v.get("compare_at_price"))

                # No MRP set means no discount, not missing data.
                # Also guard against MRP below price, which is a data error.
                if mrp is None or price is None or mrp <= price:
                    effective_mrp = price
                    discount_pct = 0.0
                else:
                    effective_mrp = mrp
                    discount_pct = round((mrp - price) / mrp * 100, 2)

                rows.append({
                    "snapshot_date": snapshot_date,
                    "brand": brand,
                    "product_id": p.get("id"),
                    "variant_id": v.get("id"),
                    "product_title": title,
                    "variant_title": v.get("title"),
                    "product_type": product_type,
                    "category": category,
                    "subcategory": subcategory,
                    "tags": tag_text,
                    "sku": v.get("sku"),
                    "price": price,
                    "compare_at_price": effective_mrp,
                    "discount_pct": discount_pct,
                    "available": bool(v.get("available")),
                    "grams": v.get("grams"),
                    "published_at": p.get("published_at"),
                    "product_created_at": p.get("created_at"),
                    # Interpretation lives in flags, never in dropped rows
                    "is_freebie": is_freebie(title, product_type, tag_text, price),
                    "is_combo": is_combo(title, product_type, tag_text),
                    "is_test": is_test(title),
                    "is_campaign": campaign_flag,
                    "is_flash_sale": flash_flag,
                })

    return rows


def apply_lost_raw_patches(df):
    """
    Restore rows for brand-dates whose raw source is permanently gone,
    from a backup of the already-parsed data taken before it was lost.

    Safe to run every time: skips a patch if the raw folder for that
    brand-date exists again (raw is always the real source of truth
    when available), and skips if the rows are already present.
    """
    for patch in LOST_RAW_PATCHES:
        brand, date = patch["brand"], patch["date"]

        # If the raw folder exists, raw wins - no patch needed.
        if (RAW_DIR / date / f"{brand}.json").exists():
            continue

        already_present = (
            (df["brand"] == brand) & (df["snapshot_date"] == date)
        ).any()
        if already_present:
            continue

        backup_file = patch["backup_file"]
        if not backup_file.exists():
            print(f"  ! Patch skipped: {backup_file} not found "
                  f"(cannot restore {brand}/{date})")
            continue

        backup = pd.read_csv(backup_file)
        to_restore = backup[
            (backup["brand"] == brand) & (backup["snapshot_date"] == date)
        ]
        if to_restore.empty:
            print(f"  ! Patch skipped: no {brand}/{date} rows in "
                  f"{backup_file}")
            continue

        df = pd.concat([df, to_restore], ignore_index=True)
        print(f"  [known gap - auto-restored] {brand}/{date}: "
              f"{len(to_restore)} rows recovered from backup")

    return df


def main():
    if not RAW_DIR.exists():
        raise SystemExit(f"{RAW_DIR} not found. Run collect.py first.")

    day_dirs = sorted(d for d in RAW_DIR.iterdir() if d.is_dir())
    if not day_dirs:
        raise SystemExit(f"No day folders in {RAW_DIR}. Run collect.py first.")

    print(f"Parsing {len(day_dirs)} day(s)\n")

    all_rows = []
    for day_dir in day_dirs:
        rows = flatten_day(day_dir)
        all_rows.extend(rows)
        print(f"  {day_dir.name}: {len(rows)} variant rows")

    df = pd.DataFrame(all_rows)
    df = apply_lost_raw_patches(df)

    # The grain we chose: one row per variant per day. Enforce it here so a
    # duplicate is caught loudly instead of quietly doubling a day's data.
    dupes = df.duplicated(subset=["variant_id", "snapshot_date"]).sum()
    if dupes:
        print(f"\nWARNING: {dupes} duplicate (variant_id, snapshot_date) rows")

    df = df.sort_values(["snapshot_date", "brand", "product_id", "variant_id"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_FILE, index=False, encoding="utf-8")

    # ---- Summary: read this every run, it is your data quality check ----
    real = df[~df["is_freebie"] & ~df["is_combo"] & ~df["is_test"]
              & ~df["is_campaign"] & ~df["is_flash_sale"]]


    print(f"\nWrote {len(df):,} rows to {OUT_FILE}")
    print(f"  days:            {df['snapshot_date'].nunique()}")
    print(f"  unique variants: {df['variant_id'].nunique():,}")
    print(f"  freebies:        {df['is_freebie'].sum():,}")
    print(f"  combos:          {df['is_combo'].sum():,}")
    print(f"  test listings:   {df['is_test'].sum():,}")
    print(f"  campaign clones: {df['is_campaign'].sum():,}")
    print(f"  flash sale:      {df['is_flash_sale'].sum():,}")
    print(f"  clean products:  {len(real):,}")

    # Flash sales are dated events - show when they ran
    flash = df[df["is_flash_sale"]]
    if len(flash):
        print("\nFlash sale listings by brand and day:")
        print(pd.crosstab(flash["snapshot_date"], flash["brand"]).to_string())

    latest = df[df["snapshot_date"] == df["snapshot_date"].max()]
    latest_real = latest[~latest["is_freebie"] & ~latest["is_combo"]
                         & ~latest["is_test"] & ~latest["is_campaign"]
                         & ~latest["is_flash_sale"]]

    print("\nLatest day - clean variants by category:")
    print(pd.crosstab(latest_real["category"], latest_real["brand"],
                      margins=True, margins_name="TOTAL").to_string())

    skin = latest_real[latest_real["category"] == "Skincare"]
    if len(skin):
        print("\nLatest day - skincare by subcategory:")
        print(pd.crosstab(skin["subcategory"], skin["brand"],
                          margins=True, margins_name="TOTAL").to_string())

    if len(real):
        print(f"\nClean product prices: "
              f"min Rs {real['price'].min():,.0f}  "
              f"median Rs {real['price'].median():,.0f}  "
              f"max Rs {real['price'].max():,.0f}")
        print(f"Out of stock: {(~real['available']).sum():,} rows "
              f"({(~real['available']).mean() * 100:.1f}%)")


if __name__ == "__main__":
    main()